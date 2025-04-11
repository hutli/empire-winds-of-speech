import asyncio
import base64
import dataclasses
import datetime
import io
import json
import multiprocessing
import os
import pathlib
import random
import time
import typing
import urllib
import uuid

import dotenv
import httpx
import pydub
import pymongo
import regex as re
import tqdm
import websockets
from bs4 import BeautifulSoup, Tag
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic.dataclasses import dataclass
from starlette.responses import FileResponse

CONFIG_DIR = pathlib.Path("config")

dotenv.load_dotenv(CONFIG_DIR / ".env")

ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
VOICES_JSON = os.environ["VOICES_JSON"]

with open(VOICES_JSON) as f:
    VOICES = json.load(f)

GLOBAL_REPLACE = [
    ("sumaah", "Suhmah"),
    ("jotun", "Jotoon"),
    ("vallorn", "Valorn"),
    # ("druj", "Drooge"),
    ("feni", "Fenni"),
    ("in-character", "incharacter"),
    ("temeschwar", "Temmeschwar"),
    ("sermersuaq", "semmersuak"),
    ("thule", "thool"),
    ("egregore", "egrigore"),
    ("(?<=\\d{3})YE", " Year of the Empire"),
    # ("(?<=\\s|\\n)OOC(?=,|;|\\.|:|\\?|!|'|\"|\\)|$|\\n|\\s)", "out of character"),
    ("yegarra", "yehgarra"),
    ("profounddecisions.co.uk", ""),
    ("mareave", "mareeve"),
]
POST_REPLACE = [
    (["Year", "of", "the", "Empire[,;.:?!'\")]*"], "YE", 1),
    # (["out", "of", "character[,;.:?!'\")]*"], "OOC", 0),
]

ARTICLES_DIR = CONFIG_DIR / "articles"
WEB_DIR = pathlib.Path("/app/web")
DB_DIR = WEB_DIR / "db"
MONGODB_DOMAIN = os.environ.get("MONGODB_DOMAIN", default="localhost")
AUDIO_DIR_NAME = "audio"
PD_URL = "https://www.profounddecisions.co.uk"
WIKI_URL = f"{PD_URL}/empire-wiki"

SECTION_TYPE_SKIP = ["img"]
MIN_TIME = 1
HOME_ID = ""
DISALLOWED_ID = "text-to-speech:disallowed"
ERROR_ID = "text-to-speech:error"

HTTP_LOOKUP = {
    "done": 200,
    "generating": 425,
    "error": 404,
    "disallowed": 400,
    ERROR_ID: 404,
    DISALLOWED_ID: 400,
}

DISALLOWED_ARTICLES = [
    "Category:.*",
    "Construct_.*",
    "Contact_Profound_Decisions",
    "Empire_rules",
    "File:.*",
    "Gazetteer",
    "Maps",
    "Nation_overview",
    "Pronunciation_guide",
    "Raise_Dawnish_army_Summer_385YE",
    "Recent_history",
    "Reconstruct_.*",
    "Safety_overview",
    "Skills",
    "Wiki_Updates",
    r"\d{3}YE_\w+_\w+_imperial_elections",
    DISALLOWED_ID,
]
MAX_SECTIONS = 200
ALLOWED_ACTIRLES = [  # Overrides the 200 section limit
    "Not_to_conquer",
]

GENERATE_ARTICLES = bool(os.getenv("GENERATE_ARTICLES", False))
REFRESH_ARTICLES = bool(os.getenv("REFRESH_ARTICLES", False))
ALWAYS_UPDATE: list[str] = [
    # DISALLOWED_ID,
    # "Ripples_and_shadows",
    # "Not_to_conquer",
    # "Thrones_and_principalities",
]
ALWAYS_REFRESH = [HOME_ID, DISALLOWED_ID, ERROR_ID]

SECTION_TYPE_PRE_DELAY = {
    "h1": 2,
    "h2": 1,
    "h3": 0.5,
    "h4": 0.5,
    "p": 0.5,
    "ol": 0.5,
    "ul": 0.5,
    "cite": 0.5,
}
OUTRO_PRE_DELAY = 2

app = FastAPI()
mongodb_client: pymongo.MongoClient = pymongo.MongoClient(MONGODB_DOMAIN, 27017)
DB = mongodb_client["database"]
COLLECTION = DB["manuscripts"]


@dataclass
class ELVoiceSettings:
    stability: float
    similarity_boost: float


@dataclass
class ELGenerationConfig:
    chunk_length_schedule: list[int]


@dataclass
class ELVoice:
    id: str
    name: str
    use: bool
    model: str
    voice_settings: ELVoiceSettings
    generation_config: ELGenerationConfig


class ElevenLabsError(Exception):
    pass


class ElevenLabsQuotaExceededError(ElevenLabsError):
    pass


class ElevenLabsSystemBusyError(ElevenLabsError):
    pass


class ElevenLabsInputTimeoutExceededError(ElevenLabsError):
    pass


ARTICLE_REPR_KEYS = ["title", "url", "sections"]


def article_repr(article: dict) -> dict:
    return {k: v for k, v in article.items() if k in ARTICLE_REPR_KEYS}


def manuscript_changed(article0: dict, article1: dict) -> bool:
    return article_repr(article0) != article_repr(article1)


def match_target_amplitude(
    sound: pydub.AudioSegment, target_dBFS: float
) -> pydub.AudioSegment:
    change_in_dBFS = target_dBFS - sound.dBFS
    return sound.apply_gain(change_in_dBFS)


async def elevenlabs_tts_alignment(
    text: str, input_voice: dict
) -> tuple[pydub.AudioSegment, list[dict]]:
    voice = ELVoice(**input_voice)
    async with websockets.connect(
        f"wss://api.elevenlabs.io/v1/text-to-speech/{voice.id}/stream-input?model_id={voice.model}"
    ) as websocket:
        body = {
            "text": text,
            "try_trigger_generation": True,
            "xi_api_key": ELEVENLABS_API_KEY,
            "voice_settings": dataclasses.asdict(voice.voice_settings),
            "generation_config": dataclasses.asdict(voice.generation_config),
        }

        await websocket.send(json.dumps(body))
        await websocket.send(json.dumps({"text": ""}))

        audio = b""
        alignment: list[dict] = []
        start = 0
        word: list[tuple[str, int]] = []
        length = 0
        while True:
            r = json.loads(await websocket.recv())
            if "error" in r:
                if r["error"] == "quota_exceeded":
                    raise ElevenLabsQuotaExceededError(r["message"])
                elif r["error"] == "system_busy":
                    raise ElevenLabsSystemBusyError(r["message"])
                elif r["error"] == "input_timeout_exceeded":
                    raise ElevenLabsInputTimeoutExceededError(r["message"])
                else:
                    raise ElevenLabsError(r)

            if r["audio"]:
                audio += base64.b64decode(r["audio"].encode())
            if r["alignment"]:
                for i, (c, a, l) in enumerate(
                    zip(
                        r["alignment"]["chars"],
                        r["alignment"]["charStartTimesMs"],
                        r["alignment"]["charDurationsMs"],
                    )
                ):
                    length += l
                    if word and c.isspace():
                        alignment.append(
                            {
                                "text": "".join(w[0] for w in word),
                                "start": word[0][1],
                                "length": max(length, MIN_TIME * 1000),
                            }
                        )
                        word = []
                        length = 0
                    else:
                        word.append((c, a + start))

                if word:
                    alignment.append(
                        {
                            "text": "".join(w[0] for w in word),
                            "start": word[0][1],
                            "length": max(length, MIN_TIME * 1000),
                        }
                    )
                word = []
                length = 0
                start += a + l
            if r["isFinal"]:
                break

        return (
            match_target_amplitude(
                pydub.effects.normalize(
                    pydub.AudioSegment.from_mp3(io.BytesIO(audio))  # type: ignore
                ),
                -20.0,
            ),
            alignment,
        )


async def elevenlabs_tts(
    text: str, voice_id: str, model_id: str = "eleven_multilingual_v1"
) -> pydub.AudioSegment:
    try:
        async with httpx.AsyncClient() as client:
            while not (
                r := await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                    json={"model_id": model_id, "text": text},
                    headers={
                        "Accept": "audio/mpeg",
                        "Content-Type": "application/json",
                        "xi-api-key": ELEVENLABS_API_KEY,
                    },
                    timeout=5 * 60,
                )
            ).is_success:
                if r.status_code in [401, 403]:
                    logger.error(f"API key failed, retrying in 10min: {r} - {r.text}")
                    await asyncio.sleep(10 * 60)
                else:
                    logger.warning(f"TTS request not successful: {r}")
                    await asyncio.sleep(10)
    except httpx.ReadTimeout:
        logger.warning(f"One ElevenLabs request timed out, retrying")
    except httpx.RemoteProtocolError:
        logger.warning(f"Server disconnected during one ElevenLabs request, retrying")

    return pydub.AudioSegment.from_mp3(io.BytesIO(r.content))  # type: ignore


def replace_sublist(
    seq: list[dict],
    search: list[str],
    replacement: str,
    offset: int,
    is_regex: bool = False,
) -> list[dict]:
    result = []
    i = 0
    while i < len(seq):
        # if sequence "text" matches search sublist replace with replacement "text"
        # but with first search elements "start" time
        if i <= (len(seq) - offset - len(search)) and all(
            [
                (
                    re.compile(s, re.IGNORECASE).match(d["text"])
                    if is_regex
                    else s.lower() == d["text"].lower()
                )
                for d, s in zip(seq[i + offset : i + offset + len(search)], search)
            ]
        ):
            result.append(
                {
                    "text": (
                        (" ".join(s["text"] for s in seq[i : i + offset]) + " ")
                        if offset
                        else ""
                    )
                    + replacement,
                    "start": seq[i]["start"],
                    "length": max(
                        sum(s["length"] for s in seq[i : i + offset + len(search)]),
                        MIN_TIME * 1000,
                    ),
                }
            )
            i += len(search) + offset
        else:
            result.append(seq[i])
            i += 1

    return result


async def generate_voice_from_text(
    text: str, voice: dict
) -> tuple[pydub.AudioSegment, list[dict]]:
    for f0, t0 in GLOBAL_REPLACE:
        text = re.compile(f0, re.IGNORECASE).sub(t0, text)

    texts = [text]
    hours = 1
    while True:
        try:
            audio, alignment = await elevenlabs_tts_alignment(text, voice)
            break
        except ElevenLabsQuotaExceededError as e:
            logger.warning(
                f"Quota exceeded, waiting {hours} hours for quota reset: {e}"
            )
            await asyncio.sleep(hours * 60 * 60)
            hours = min(24, hours + 1)
        except ElevenLabsSystemBusyError as e:
            logger.warning(
                f"Elevenlabs servers busy, waiting 10s for them to catch up: {e}"
            )
            await asyncio.sleep(10)
        except ElevenLabsInputTimeoutExceededError as e:
            logger.warning(f"Mistiming of input text, retrying in 10s: {e}")
            await asyncio.sleep(10)
        except websockets.exceptions.ConnectionClosedError as e:
            logger.warning(
                f"Websocket connection closed unexpectedly, trying again in 10s: {e}"
            )
            await asyncio.sleep(10)

    for f1, t1, offset in POST_REPLACE:
        alignment = replace_sublist(alignment, f1, t1, offset, True)

    return audio, alignment


def text_to_spans(text: str | list[str]) -> list:
    return [
        {"text": t.replace(" ", "").replace("–", "-").strip()}
        for t in (text.split() if isinstance(text, str) else text)
    ]


def generate_error_manuscript(article_id: str) -> dict:
    article_url = f"{WIKI_URL}/{article_id}"

    res_dir = DB_DIR / ERROR_ID
    res_dir.mkdir(parents=True, exist_ok=True)

    audio_dir = res_dir / AUDIO_DIR_NAME
    audio_dir.mkdir(parents=True, exist_ok=True)

    article = {
        "title": article_id.replace("_", " "),
        "url": None,
        "state": "error" if article_id != ERROR_ID else "done",
        "forced_voice": "Ella",
        "sections": [
            {
                "section_type": "h1",
                "audio_path": str((audio_dir / f"{0:04}.mp3").absolute()),
                "audio_url": f'/{((audio_dir / f"{0:04}.mp3").relative_to(WEB_DIR))}',
                "alignment_path": str((audio_dir / f"{0:04}.json").absolute()),
                "alignment_url": f'/{((audio_dir / f"{0:04}.json").relative_to(WEB_DIR))}',
                "spans": text_to_spans("Error"),
            }
        ]
        + [
            {
                "section_type": "p",
                "audio_path": str((audio_dir / f"{i+1:04}.mp3").absolute()),
                "audio_url": f'/{((audio_dir / f"{i+1:04}.mp3").relative_to(WEB_DIR))}',
                "alignment_path": str((audio_dir / f"{i+1:04}.json").absolute()),
                "alignment_url": f'/{((audio_dir / f"{i+1:04}.json").relative_to(WEB_DIR))}',
                "spans": text_to_spans(s),
            }
            for i, s in enumerate(
                [
                    "The system could not process this article. Either the article does not exist, or an error occurred during the download. The system will continue to attempt to process the article, in case the problem is temporary.",
                ]
            )
        ],
        "outro": {
            "audio_path": str((audio_dir / "outro.mp3").absolute()),
            "audio_url": "/" + str((audio_dir / "outro.mp3").relative_to(WEB_DIR)),
        },
    }
    if article_id != ERROR_ID:
        article["url"] = f"{WIKI_URL}/{article_id}"
    return article


def generate_disallowed_manuscript(article_id: str) -> dict:
    article_url = f"{WIKI_URL}/{article_id}"

    res_dir = DB_DIR / DISALLOWED_ID
    res_dir.mkdir(parents=True, exist_ok=True)

    audio_dir = res_dir / AUDIO_DIR_NAME
    audio_dir.mkdir(parents=True, exist_ok=True)

    article = {
        "title": article_id.replace("_", " "),
        "url": None,
        "state": "disallowed" if article_id != DISALLOWED_ID else "done",
        "forced_voice": "Ella",
        "sections": [
            {
                "section_type": "h1",
                "audio_path": str((audio_dir / f"{0:04}.mp3").absolute()),
                "audio_url": f'/{((audio_dir / f"{0:04}.mp3").relative_to(WEB_DIR))}',
                "alignment_path": str((audio_dir / f"{0:04}.json").absolute()),
                "alignment_url": f'/{((audio_dir / f"{0:04}.json").relative_to(WEB_DIR))}',
                "spans": text_to_spans("Disallowed article"),
            }
        ]
        + [
            {
                "section_type": "p",
                "audio_path": str((audio_dir / f"{i+1:04}.mp3").absolute()),
                "audio_url": f'/{((audio_dir / f"{i+1:04}.mp3").relative_to(WEB_DIR))}',
                "alignment_path": str((audio_dir / f"{i+1:04}.json").absolute()),
                "alignment_url": f'/{((audio_dir / f"{i+1:04}.json").relative_to(WEB_DIR))}',
                "spans": text_to_spans(s),
            }
            for i, s in enumerate(
                [
                    "This article is too long or unnecessary. The purpose of this system is to help other people and myself better understand the world of Empire. It is created and maintained out of the goodwill of a single player, and I do it entirely in my spare time without any help from Profound Decisions.",
                    "I have no security protections, captchas, anti-DDOS, fancy load-balancing, IP registration, cookies, or anything else - the system's viability relies entirely on its users not abusing it. Unfortunately, I have experienced some people abusing the system a bit, so I've been forced to start disallowing some articles.",
                    "This article has been deemed unfit for text-to-speech, either automatically or directly by me. This is likely because it is either an internal Wiki-specific article, too long compared to how often it is updated, makes no sense as text-to-speech, or is generally unnecessary to understand the world and game of Empire.",
                    "I try only to exclude an absolute minimum of articles, so if you think this is a mistake and the article should still have text-to-speech, please get in touch with me either by email (click the letter at the bottom right) or by finding me during out-of-character time at any of the Empire events (Bloodcrow Knott, Imperial Orcs).",
                ]
            )
        ],
        "outro": {
            "audio_path": str((audio_dir / "outro.mp3").absolute()),
            "audio_url": "/" + str((audio_dir / "outro.mp3").relative_to(WEB_DIR)),
        },
    }
    if article_id != DISALLOWED_ID:
        article["url"] = f"{WIKI_URL}/{article_id}"
    return article


def has_class(content: Tag, class_name: str) -> bool:
    return bool(
        content.attrs and "class" in content.attrs and class_name in content["class"]
    )


def content_to_sections(
    content: Tag, audio_dir: pathlib.Path
) -> typing.Generator[dict, None, None]:
    i = 0
    for child in content.findChildren(recursive=False):
        text = None
        children = child.findChildren(recursive=False)

        if child.name == "ul" or child.name == "ol":
            text = [c.text.strip() for c in children if c.text.strip()]
        elif child.name == "div" and has_class(child, "ic"):
            if len(children) != 1:
                logger.error(
                    f"Error while parsing ic section (in-character quote)! Expected exactly 1 child, got {len(child.children)}"
                )
                return
            child = children[0]

            tmp_sections = []
            if has_class(child, "quote"):
                tmp_sections = [c.text for c in child.children]
            else:
                tmp_sections = child.text.split("\n")

            for c in tmp_sections:
                if block := c.strip():
                    yield {
                        "section_type": "cite",
                        "audio_path": str((audio_dir / f"{i+1:04}.mp3").absolute()),
                        "audio_url": "/"
                        + str((audio_dir / f"{i+1:04}.mp3").relative_to(WEB_DIR)),
                        "alignment_path": str(
                            (audio_dir / f"{i+1:04}.json").absolute()
                        ),
                        "alignment_url": "/"
                        + str((audio_dir / f"{i+1:04}.json").relative_to(WEB_DIR)),
                        "spans": text_to_spans(block),
                    }
                    i += 1
        else:
            text = child.text.strip()

        if text:
            yield {
                "section_type": child.name,
                "audio_path": str((audio_dir / f"{i+1:04}.mp3").absolute()),
                "audio_url": "/"
                + str((audio_dir / f"{i+1:04}.mp3").relative_to(WEB_DIR)),
                "alignment_path": str((audio_dir / f"{i+1:04}.json").absolute()),
                "alignment_url": "/"
                + str((audio_dir / f"{i+1:04}.json").relative_to(WEB_DIR)),
                "spans": text_to_spans(text),
            }
            i += 1


def generate_manuscript(
    article_id: str, res_dir: pathlib.Path, audio_dir: pathlib.Path
) -> dict:
    url = f"{WIKI_URL}/{article_id}"

    if (
        any(
            r
            for r in DISALLOWED_ARTICLES
            if re.compile(r, re.IGNORECASE).match(article_id)
        )
        and article_id not in ALLOWED_ACTIRLES
    ):
        logger.warning(f'"{article_id}" is disallowed')
        return generate_disallowed_manuscript(article_id)

    if article_id == HOME_ID:
        return {
            "title": "Empire Wikipedia Winds of Speech",
            "url": None,
            "state": "done",
            "forced_voice": "Ella",
            "sections": [
                {
                    "section_type": "h1",
                    "audio_path": str((audio_dir / f"{0:04}.mp3").absolute()),
                    "audio_url": "/"
                    + str((audio_dir / f"{0:04}.mp3").relative_to(WEB_DIR)),
                    "alignment_path": str((audio_dir / f"{0:04}.json").absolute()),
                    "alignment_url": "/"
                    + str((audio_dir / f"{0:04}.json").relative_to(WEB_DIR)),
                    "spans": text_to_spans("Empire Wikipedia Winds of Speech"),
                },
                *[
                    {
                        "section_type": "p",
                        "audio_path": str((audio_dir / f"{i+1:04}.mp3").absolute()),
                        "audio_url": "/"
                        + str((audio_dir / f"{i+1:04}.mp3").relative_to(WEB_DIR)),
                        "alignment_path": str(
                            (audio_dir / f"{i+1:04}.json").absolute()
                        ),
                        "alignment_url": "/"
                        + str((audio_dir / f"{i+1:04}.json").relative_to(WEB_DIR)),
                        "spans": text_to_spans(text),
                    }
                    for i, text in enumerate(
                        [
                            "Welcome to the unofficial Empire Wikipedia Winds of Speech!",
                            "This is an unofficial text-to-speech tool to help better focus on and understand the articles on the Empire Wikipedia.",
                            'It is pretty simple to use: When you find an article on the Empire Wikipedia you would like to listen to and read along with, add a "p" to the start of the URL. You\'ll then go directly to the text-to-speech article on this website (see the video clip below). If the article seems outdated, it may be because you are the first to visit it in a while, so please let the system update the article - this can take a bit.',
                        ]
                    )
                ],
                {
                    "section_type": "img",
                    "src": "img/tts.gif",
                    "alt": "A video clip illustrating how to access text-to-speech directly from the Empire Wikipedia.",
                    "spans": [],
                },
                *[
                    {
                        "section_type": "p",
                        "audio_path": str((audio_dir / f"{i+5:04}.mp3").absolute()),
                        "audio_url": "/"
                        + str((audio_dir / f"{i+5:04}.mp3").relative_to(WEB_DIR)),
                        "alignment_path": str(
                            (audio_dir / f"{i+5:04}.json").absolute()
                        ),
                        "alignment_url": "/"
                        + str((audio_dir / f"{i+5:04}.json").relative_to(WEB_DIR)),
                        "spans": text_to_spans(text),
                    }
                    for i, text in enumerate(
                        [
                            "The system was initially designed for personal use, but after making it publicly available, I've received some valuable suggestions. Some are now part of the accessibility settings in the left side burger menu; some have changed how articles are generated, shown, and read aloud; and some have changed the navigation buttons and sliders. Please share suggestions and any improvements you'd like to see - either by email (click the letter at the bottom right) or by finding me during out-of-character time at any of the Empire events (Bloodcrow Knott, Imperial Orcs).",
                            "If you want to support me, you can buy me a coffee or beer in the field or donate by clicking the coffee cup on the bottom right.",
                            "I hope this can help others who struggle as much with reading the Wikipedia as I have!",
                        ]
                    )
                ],
            ],
            "outro": {
                "audio_path": str((audio_dir / "outro.mp3").absolute()),
                "audio_url": "/" + str((audio_dir / "outro.mp3").relative_to(WEB_DIR)),
            },
        }

    try:
        response = httpx.get(url, verify=False, timeout=60)
    except Exception as e:
        logger.error(f'Could not get article "{url}": {e}')
        return generate_error_manuscript(article_id)

    if not response.is_success:
        logger.error(f'Could not get article "{url}": {response}')
        return generate_error_manuscript(article_id)

    soup = BeautifulSoup(response.text, "html.parser")
    content = soup.find("div", {"id": "mw-content-text"})

    if not isinstance(content, Tag):
        logger.error(f'Soup for "{url}" does not contain ID "mw-content-text"')
        return generate_error_manuscript(article_id)

    # Get image
    img_tag = content.find("img")
    img_url = f"{PD_URL}{img_tag['src']}" if isinstance(img_tag, Tag) else None

    while isinstance(content, Tag) and len(list(content.children)) == 1:
        content = list(content.children)[0]  # type: ignore

    title_tag = soup.find("h1")
    if not isinstance(title_tag, Tag):
        logger.error(f'Soup for "{url}" does not contain h1 header')
        return generate_error_manuscript(article_id)
    title = title_tag.text.strip()

    toc = content.find("div", {"id": "toc"})
    if isinstance(toc, Tag):
        toc.decompose()  # remove Table of Content

    for child in content.findChildren("div", recursive=False):
        if (
            not isinstance(child, Tag)
            or not child.attrs
            or "class" not in child.attrs
            or "ic" not in child["class"]
        ):
            child.decompose()
    for child in content.find_all("sup"):
        child.decompose()
    for child in content.find_all("table"):
        child.decompose()

    (audio_dir / f"{0:04}").mkdir(parents=True, exist_ok=True)

    sections = list(content_to_sections(content, audio_dir))

    article = {
        "title": title,
        "url": url,
        "state": "generating",
        "sections": [
            {
                "section_type": "h1",
                "audio_path": str((audio_dir / f"{0:04}.mp3").absolute()),
                "audio_url": "/"
                + str((audio_dir / f"{0:04}.mp3").relative_to(WEB_DIR)),
                "alignment_path": str((audio_dir / f"{0:04}.json").absolute()),
                "alignment_url": "/"
                + str((audio_dir / f"{0:04}.json").relative_to(WEB_DIR)),
                "spans": text_to_spans(title),
            },
            *sections,
        ],
        "outro": {
            "audio_path": str((audio_dir / "outro.mp3").absolute()),
            "audio_url": "/" + str((audio_dir / "outro.mp3").relative_to(WEB_DIR)),
        },
    }

    # Add image
    if img_url:
        article["img"] = img_url
    return article


def generate_complete_audio(article_id: str) -> None:
    article_id = article_id.replace(" ", "_")
    manuscript = COLLECTION.find_one({"_id": article_id})
    sound = None
    if manuscript and manuscript["state"] != "generating":
        for section in manuscript["sections"]:
            if section["section_type"] not in SECTION_TYPE_SKIP:
                if sound:
                    if section["section_type"] not in SECTION_TYPE_PRE_DELAY:
                        logger.warning(
                            f'"{section["section_type"]}" not in SECTION_TYPE_PRE_DELAY! Using default 1s'
                        )
                        sound = sound.append(
                            pydub.AudioSegment.silent(duration=1000), crossfade=0
                        )
                    else:
                        sound = sound.append(
                            pydub.AudioSegment.silent(
                                duration=SECTION_TYPE_PRE_DELAY[section["section_type"]]
                                * 1000
                            ),
                            crossfade=0,
                        )
                else:
                    sound = pydub.AudioSegment.silent(duration=0)

                sound = sound.append(
                    pydub.AudioSegment.from_mp3(section["audio_path"]), crossfade=0
                )

    else:
        raise Exception(f"Article not yet generated!")

    if not sound:
        logger.error(f'No sections in "{article_id}"!')
        return

    if "outro" in manuscript and "audio_path" in manuscript["outro"]:
        sound.append(
            pydub.AudioSegment.silent(duration=OUTRO_PRE_DELAY * 1000), crossfade=0
        )
        sound.append(
            pydub.AudioSegment.from_mp3(manuscript["outro"]["audio_path"]),
            crossfade=0,
        )

    res_dir = DB_DIR / article_id
    res_dir.mkdir(parents=True, exist_ok=True)

    audio_dir = res_dir / AUDIO_DIR_NAME
    audio_dir.mkdir(parents=True, exist_ok=True)

    audio_path = audio_dir / f"{article_id}.mp3"
    sound.export(audio_path, format="mp3")

    COLLECTION.update_one(
        {"_id": manuscript["_id"]},
        {
            "$set": {
                "complete_audio_path": str(audio_path.absolute()),
                "complete_audio_url": f"/{audio_path.relative_to(WEB_DIR)}",
            }
        },
    )


def generate_audio(manuscript: dict, task: str) -> None:
    voice = random.choice([v for v in VOICES if v["use"]])
    if "forced_voice" in manuscript:
        v = next((v for v in VOICES if v["name"] == manuscript["forced_voice"]), None)
        if v:
            voice = v
        else:
            logger.warning(
                f'Forced voice "{manuscript["forced_voice"]}" does not exist in config, please add'
            )

    logger.info(f'Chose voice "{voice["name"]}" for "{manuscript["title"]}"')

    for i, section in enumerate(manuscript["sections"]):
        COLLECTION.update_one(
            {"_id": manuscript["_id"]},
            {"$set": {"progress": i / len(manuscript["sections"])}},
        )

        if text := " ".join(s["text"] for s in section["spans"]).strip():
            audio, alignment = asyncio.run(generate_voice_from_text(text, voice))
            if section["section_type"] == "ul" or section["section_type"] == "ol":
                for s in section["spans"]:
                    alignment = replace_sublist(
                        alignment, s["text"].split(), s["text"], 0, False
                    )

            audio.export(section["audio_path"])
            json.dump(alignment, open(section["alignment_path"], "w"))
            logger.info(
                f'{i}/{len(manuscript["sections"])-1} TTS audio segments generated for "{manuscript["title"]}"'
            )
        else:
            logger.info(
                f'{i}/{len(manuscript["sections"])-1} TTS audio segments generated for "{manuscript["title"]}"'
            )
    audio, _ = asyncio.run(
        generate_voice_from_text(
            f'This article was read aloud by the artificial voice, "{voice["name"]}".'
            + (
                " All content of this article is the original work of Profound Decisions and can be found on the Empire wikipedia."
                if manuscript["_id"] not in [HOME_ID, DISALLOWED_ID, ERROR_ID]
                else ""
            )
            + " Thank you for listening.",
            voice,
        )
    )
    audio.export(manuscript["outro"]["audio_path"])
    logger.info(f'All TTS audio segments generated for "{manuscript["title"]}"')


def insert_or_replace(manuscript: dict) -> None:
    try:
        COLLECTION.insert_one(manuscript)
    except pymongo.errors.DuplicateKeyError:
        COLLECTION.replace_one({"_id": manuscript["_id"]}, manuscript)


def update_manuscript(manuscript: dict, task: str = "Updating manuscript") -> None:
    generate_audio(manuscript, task)
    manuscript["state"] = "done"
    manuscript["lastmod"] = datetime.datetime.now()
    insert_or_replace(manuscript)

    logger.info(f'Generating complete audio file for "{manuscript["title"]}"')
    generate_complete_audio(manuscript["_id"])
    logger.info(f'Complete audio file generated for "{manuscript["title"]}"')


def get_article(article_id: str) -> typing.Any:
    article_id = article_id.replace(" ", "_")
    if not article_id:
        article_id = HOME_ID
    return COLLECTION.find_one({"_id": article_id})


def article_processor(queue: multiprocessing.Queue) -> None:
    while True:
        article_id = queue.get(block=True, timeout=None)

        logger.info(
            f'Processing "{article_id}" ({queue.qsize()} articles left in queue)'
        )
        if not GENERATE_ARTICLES:
            logger.info(
                f'Article generation disabled, skipping "{article_id}" ({queue.qsize()} articles left in queue)'
            )
            continue

        res_dir = DB_DIR / article_id
        res_dir.mkdir(parents=True, exist_ok=True)

        audio_dir = res_dir / AUDIO_DIR_NAME
        audio_dir.mkdir(parents=True, exist_ok=True)

        try:
            manuscript = {
                "_id": article_id,
                **generate_manuscript(article_id, res_dir, audio_dir),
            }

            if manuscript["state"] == "disallowed":
                manuscript["lastmod"] = datetime.datetime.now()
                insert_or_replace(manuscript)
                queue.put(DISALLOWED_ID)
                continue
            elif manuscript["state"] == "error":
                manuscript["lastmod"] = datetime.datetime.now()
                insert_or_replace(manuscript)
                queue.put(ERROR_ID)
                continue

            existing_manuscript = COLLECTION.find_one({"_id": article_id})

            if existing_manuscript is not None:
                if manuscript["_id"] in ALWAYS_UPDATE:
                    logger.warning(
                        f'Article "{manuscript["title"]}" ({manuscript["_id"]}) in "always update", updating manuscript'
                    )
                    update_manuscript(manuscript)
                elif (
                    "state" in existing_manuscript
                    and existing_manuscript["state"] == "generating"
                ):
                    logger.warning(
                        f'Article "{manuscript["title"]}" ({manuscript["url"]}) interrupted during generation, re-generating manuscript'
                    )
                    update_manuscript(manuscript)
                elif len(manuscript["sections"]) + 1 > len(list(audio_dir.iterdir())):
                    logger.warning(
                        f'Article "{manuscript["title"]}" ({manuscript["url"]}) has fewer generated files ({len(list(audio_dir.iterdir()))}) than needed ({len(manuscript["sections"]) + 1}), regenerating files'
                    )
                    update_manuscript(manuscript)
                elif manuscript_changed(manuscript, existing_manuscript):
                    if REFRESH_ARTICLES or manuscript["_id"] in ALWAYS_REFRESH:
                        logger.info(
                            f'Article "{manuscript["title"]}" ({manuscript["url"]}) changed, updating manuscript'
                        )
                        update_manuscript(manuscript)
                    else:
                        logger.warning(
                            f'Article "{manuscript["title"]}" ({manuscript["url"]}) changed, but manuscript updating disabled - skipping'
                        )
                else:
                    logger.info(
                        f'Article "{manuscript["title"]}" ({manuscript["url"]}) unchanged, skipping'
                    )

            else:
                logger.info(
                    f'Article "{manuscript["title"]}" ({manuscript["url"]}) not yet generated, generating manuscript'
                )
                update_manuscript(manuscript, "Generating manuscript")
        except httpx.ConnectError as e:
            logger.warning(f'Could not GET article "{article_id}": {e}')

        if (
            (a := COLLECTION.find_one({"_id": article_id}))
            and isinstance(a, dict)
            and "complete_audio_url" not in a
        ):
            try:
                generate_complete_audio(manuscript["_id"])
            except Exception as e:
                logger.error(
                    f'Article "{manuscript["title"]}" has breaking errors, force-updating manuscript - "{type(e)}: {e}"'
                )
                update_manuscript(manuscript, "Manuscript error")
                generate_complete_audio(manuscript["_id"])


article_queue: multiprocessing.Queue = multiprocessing.Queue()
multiprocessing.Process(target=article_processor, args=(article_queue,)).start()


@app.get("/sitemap.xml")
def sitemap() -> Response:
    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for manuscript in tqdm.tqdm(
        list(sorted(COLLECTION.find(), key=lambda a: a["_id"])),
        desc="Building sitemap.xml",
    ):
        if manuscript["state"] == "done":
            sitemap += "\n	<url>"
            sitemap += f"\n		<loc>https://www.pprofounddecisions.co.uk/{"empire-wiki/" if manuscript["_id"] else ""}{urllib.parse.quote_plus(manuscript["_id"])}</loc>"
            sitemap += (
                f"\n		<lastmod>{manuscript["lastmod"].date().isoformat()}</lastmod>"
            )
            sitemap += f"\n		<changefreq>monthly</changefreq>"
            sitemap += "\n	</url>"
            sitemap += "\n"
    sitemap += "\n</urlset>"
    sitemap += "\n"

    return Response(content=sitemap, media_type="application/xml")


@app.get("/empire-wiki/{article_id:path}")
def index(article_id: str) -> HTMLResponse:
    with open(WEB_DIR / "index.html") as f:
        index = f.read()
    article = get_article(article_id)
    if article:
        if "title" in article and article["title"]:
            index = index.replace(
                "Empire Wikipedia Winds of Speech",
                f"Empire Wikipedia Winds of Speech - {article["title"]}",
            )
        if "img" in article and article["img"]:
            index = index.replace(
                "https://www.pprofounddecisions.co.uk/meta.png", article["img"]
            )

        replaced_meta = False
        article_content = ""
        if "sections" in article and article["sections"]:
            for i, section in enumerate(article["sections"]):
                article_content += f'<{section["section_type"]}>'
                section_content = " ".join(s["text"] for s in section["spans"])
                article_content += section_content
                article_content += f"</{section["section_type"]}>"
                if not replaced_meta and section["section_type"] == "p":
                    index = index.replace(
                        "An unofficial text-to-speech system for the Empire Wikipedia.",
                        f"{section_content}\n\nBrought to you by: Empire Wikipedia Winds of Speech - An unofficial text-to-speech system for the Empire Wikipedia. ",
                    )
                    replaced_meta = True

            index = (
                index.split('article-content">', 1)[0]
                + f'article-content">{article_content}'
                + index.split('article-content">', 1)[1]
            )

    return HTMLResponse(
        content=index,
        status_code=(
            HTTP_LOOKUP[article["_id"]]
            if article and article["_id"] in HTTP_LOOKUP
            else (
                HTTP_LOOKUP[article["state"]]
                if article and "state" in article and article["state"] in HTTP_LOOKUP
                else 404
            )
        ),
    )


@app.get("/")
def home() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/manuscript/{article_id:path}")
def manuscript(article_id: str) -> typing.Any:
    manuscript = get_article(article_id)
    article_queue.put(article_id)
    if manuscript is not None:
        return manuscript
    else:
        insert_or_replace(
            {
                "_id": article_id,
                "progress": 0.0,
                "title": article_id,
                "url": f"{WIKI_URL}/{article_id}",
                "state": "generating",
                "sections": [
                    {
                        "section_type": "h1",
                        "spans": [{"text": article_id}],
                    },
                    {
                        "section_type": "p",
                        "spans": [
                            {"text": "The system is still processing this article."},
                            {
                                "text": "This will take anywhere from a couple of minutes to hours, depending on the article and how many articles are ahead of this one in the queue."
                            },
                            {
                                "text": "You are welcome to come back to check the progress, but unfortunately the system is not smart enough to give you an estimate."
                            },
                        ],
                    },
                ],
            }
        )
        return {
            "title": article_id,
            "url": f"{WIKI_URL}/{article_id}",
            "state": "generating",
            "sections": [
                {
                    "section_type": "h1",
                    "spans": [{"text": "New Article!"}],
                },
                {
                    "section_type": "p",
                    "spans": [
                        {
                            "text": "Congratulations! You are the first to visit this article!"
                        }
                    ],
                },
                {
                    "section_type": "p",
                    "spans": [
                        {
                            "text": "Unfortunately, this means that the system has not yet generated this article."
                        },
                        {
                            "text": "This it will take anywhere from a couple of minutes to hours, depending on the article and how many articles are ahead of this one in the queue."
                        },
                        {
                            "text": "You are welcome to come back later to check again but unfortunately the system is not smart enough to give you an estimate."
                        },
                    ],
                },
            ],
        }


@app.get("/api/complete_audio/{article_id:path}")
def complete_audio(article_id: str) -> str:
    manuscript = get_article(article_id)
    if not isinstance(manuscript, dict) or manuscript["state"] != "done":
        raise Exception("Article not generated")
    if "complete_audio_url" not in manuscript:
        generate_complete_audio(article_id)

    manuscript = get_article(article_id)
    assert isinstance(manuscript, dict)
    return str(manuscript["complete_audio_url"])


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="Web")
