import asyncio
import base64
import datetime
import http
import io
import json
import multiprocessing
import os
import pathlib
import random
import time
import typing
import urllib

import bs4
import dateutil.parser
import dotenv
import fastapi
import fastapi.responses
import fastapi.staticfiles
import httpx
import pydub
import pymongo
import regex
import starlette.responses
import tqdm
import websockets
from loguru import logger
from pydantic.dataclasses import dataclass

CONFIG_DIR = pathlib.Path("config")
WEB_DIR = pathlib.Path("web")
DB_DIR = WEB_DIR / "db"

dotenv.load_dotenv(CONFIG_DIR / ".env")

ELEVENLABS_API_KEYS_JSON = CONFIG_DIR / "el_api_keys.json"
ELEVENLABS_FRAME_RATE = 44_100
ELEVENLABS_BITRATE = 128_000
ELEVENLABS_CHANNELS = 1
SAFE_QUOTA_MARGIN = int(os.environ["SAFE_QUOTA_MARGIN"])
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

MONGODB_DOMAIN = os.environ.get("MONGODB_DOMAIN", default="localhost")
AUDIO_DIR_NAME = "audio"
PD_URL = "https://www.profounddecisions.co.uk"
WIKI_URL = f"{PD_URL}/empire-wiki"
API_URL = f"{PD_URL}/mediawiki-public/api.php"

SECTION_TYPE_SKIP = ["img"]
MIN_TIME = 1
HOME_ID = ""
DISALLOWED_ID = "text-to-speech:disallowed"
ERROR_ID = "text-to-speech:error"

HTTP_LOOKUP = {
    "done": http.HTTPStatus.OK,
    "generating": http.HTTPStatus.TOO_EARLY,
    "error": http.HTTPStatus.NOT_FOUND,
    "disallowed": http.HTTPStatus.BAD_REQUEST,
    ERROR_ID: http.HTTPStatus.NOT_FOUND,
    DISALLOWED_ID: http.HTTPStatus.BAD_REQUEST,
}

DISALLOWED_ARTICLES = [
    regex.compile(r, regex.IGNORECASE)
    for r in [
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
]

GENERATE_ARTICLES = bool(os.getenv("GENERATE_ARTICLES", False))
REFRESH_ARTICLES = bool(os.getenv("REFRESH_ARTICLES", False))
ALWAYS_UPDATE: list[str] = json.loads(os.getenv("ALWAYS_UPDATE", "[]"))
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
OUTRO_POST_SILENCE = 4

CHAPTER_TYPE = "h2"

APP = fastapi.FastAPI()
DB_CLIENT: pymongo.MongoClient = pymongo.MongoClient(MONGODB_DOMAIN, 27017)
DB = DB_CLIENT["database"]
COLLECTION = DB["manuscripts"]
META = DB["meta"]


# @dataclass
# class ELVoiceSettings:
#     stability: float
#     similarity_boost: float
#     use_speaker_boost: bool


# @dataclass
# class ELGenerationConfig:
#     chunk_length_schedule: list[int]


@dataclass
class ELVoice:
    id: str | list[str]
    nickname: str
    use: bool
    model: str
    name: str | None = None
    # voice_settings: ELVoiceSettings | None = None
    # generation_config: ELGenerationConfig | None = None


@dataclass
class APIKey:
    username: str
    key: str
    use: bool


class ElevenLabsError(Exception):
    pass


class ElevenLabsQuotaExceededError(ElevenLabsError):
    pass


class ElevenLabsSafeQuotaStop(ElevenLabsError):
    pass


class ElevenLabsSystemBusyError(ElevenLabsError):
    pass


class ElevenLabsInputTimeoutExceededError(ElevenLabsError):
    pass


class ElevenLabsSomethingWentWrong(ElevenLabsError):
    pass


class ElevenLabsVoiceIdDoesNotExist(ElevenLabsError):
    def __init__(self, message: str, voice: ELVoice):
        super().__init__(message)
        self.voice = voice


class ElevenLabsDetectedUnusualActivity(ElevenLabsError):
    pass


class ElevenLabsInvalidStatus(ElevenLabsError):
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


def my_url(url: str) -> str:
    url = url.split("#")[0]
    url = url.replace("?", "%3F")
    return url


async def elevenlabs_tts_alignment(
    text: str, voice: ELVoice, elevenlabs_api_key: str
) -> tuple[pydub.AudioSegment, list[dict]]:
    try:
        while True:
            try:
                user_subscription_r = httpx.get(
                    "https://api.elevenlabs.io/v1/user/subscription",
                    headers={"xi-api-key": elevenlabs_api_key},
                    timeout=60,
                )
                break
            except Exception as e:
                logger.error(f"Could not get EL subscription, retrying in 10 min: {e}")
                await asyncio.sleep(10 * 60)
        if user_subscription_r.is_success:
            user_subscription = user_subscription_r.json()
            if (
                user_subscription["character_count"]
                > user_subscription["character_limit"] - SAFE_QUOTA_MARGIN
            ):
                raise ElevenLabsSafeQuotaStop(
                    f'Character count ({user_subscription["character_count"]}) too close to character limit ({user_subscription["character_limit"]}, making safe quota stop ({user_subscription["character_limit"]-user_subscription["character_count"]}/{SAFE_QUOTA_MARGIN})'
                )
        else:
            logger.error(user_subscription_r.json()["detail"]["message"])

        url = f"wss://api.elevenlabs.io/v1/text-to-speech/{voice.id}/stream-input?output_format=mp3_{ELEVENLABS_FRAME_RATE}_{ELEVENLABS_BITRATE//1000}&model_id={voice.model}"
        async with websockets.connect(url) as websocket:
            body = {
                "text": text,
                "try_trigger_generation": True,
                "xi-api-key": elevenlabs_api_key,
            }
            # if voice.settings:
            #     body["settings"] = dataclasses.asdict(voice.settings)
            # if voice.generation_config:
            #     body["generation_config"] = dataclasses.asdict(voice.generation_config)

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
                    elif r["error"] == "something_went_wrong":
                        raise ElevenLabsSomethingWentWrong(r["message"])
                    elif r["error"] == "voice_id_does_not_exist":
                        raise ElevenLabsVoiceIdDoesNotExist(r["message"], voice)
                    elif r["error"] == "detected_unusual_activity":
                        raise ElevenLabsDetectedUnusualActivity(r["message"])
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
    except websockets.exceptions.InvalidStatus as e:
        raise ElevenLabsInvalidStatus(e)
    with open("tmp.mp3", "wb") as f:
        f.write(audio)
    a = pydub.AudioSegment.from_file("tmp.mp3", format="mp3")
    # a = pydub.AudioSegment.from_mp3(io.BytesIO(audio))  # , bitrate="128k")
    # a = a.set_frame_rate(ELEVENLABS_FRAME_RATE)
    # a = a.set_channels(ELEVENLABS_CHANNELS)
    return (
        a,
        # pydub.effects.normalize(a),
        alignment,
    )
    # return (
    #     match_target_amplitude(
    #         pydub.effects.normalize(pydub.AudioSegment.from_mp3(io.BytesIO(audio))),
    #         -20.0,
    #     ),
    #     alignment,
    # )


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
                    regex.compile(s, regex.IGNORECASE).match(d["text"])
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


API_KEY_POINTER = 0


async def generate_voice_from_text(
    text: str, voice: ELVoice, api_keys: list[APIKey]
) -> tuple[pydub.AudioSegment, list[dict]]:
    global API_KEY_POINTER
    for f0, t0 in GLOBAL_REPLACE:
        text = regex.compile(f0, regex.IGNORECASE).sub(t0, text)

    texts = [text]
    hours = 1
    while True:
        while not api_keys[API_KEY_POINTER].use:
            API_KEY_POINTER = (
                API_KEY_POINTER + 1 if API_KEY_POINTER + 1 < len(api_keys) else 0
            )

        # SUCCESS
        try:
            audio, alignment = await elevenlabs_tts_alignment(
                text, voice, api_keys[API_KEY_POINTER].key
            )
            for f1, t1, offset in POST_REPLACE:
                alignment = replace_sublist(alignment, f1, t1, offset, True)

            return audio, alignment
        # TEMP ERROR
        except ElevenLabsVoiceIdDoesNotExist as e:
            logger.warning(
                f'"{api_keys[API_KEY_POINTER].username}" does not recognise ID "{voice.id}"; please make sure you have added the voice "{voice.name}" to your library and the ID is correct. Retrying in 10 min: {e}'
            )
            await asyncio.sleep(10 * 60)
        except ElevenLabsSystemBusyError as e:
            logger.warning(
                f"Elevenlabs servers busy, waiting 10 seconds for them to catch up: {e}"
            )
            await asyncio.sleep(10)
        except ElevenLabsInputTimeoutExceededError as e:
            logger.warning(f"Mistiming of input text, retrying in 10 seconds: {e}")
            await asyncio.sleep(10)
        except ElevenLabsSomethingWentWrong as e:
            logger.warning(f"Something went wrong, retrying in 10 min: {e}")
            await asyncio.sleep(10 * 60)
        except websockets.exceptions.ConnectionClosedError as e:
            logger.warning(
                f"Websocket connection closed unexpectedly, trying again in 10 seconds: {e}"
            )
            await asyncio.sleep(10)
        # API KEY ERRORS
        except (ElevenLabsQuotaExceededError, ElevenLabsSafeQuotaStop) as e:
            API_KEY_POINTER += 1
            if API_KEY_POINTER < len(api_keys):
                logger.warning(
                    f'"{api_keys[API_KEY_POINTER-1].username}" out of quota, trying next key: {e}'
                )
                if isinstance(e, ElevenLabsQuotaExceededError):
                    await asyncio.sleep(random.randint(10, 60))
            else:
                API_KEY_POINTER = 0
                logger.warning(
                    f"All API Keys out of quota - waiting {hours} hours for quota reset"
                )
                await asyncio.sleep(hours * 60 * 60)
                hours = min(24, hours + 1)

        except ElevenLabsDetectedUnusualActivity as e:
            logger.warning(
                f'Unusual activity detected for "{api_keys[API_KEY_POINTER].username}", disabling API Key!'
            )
            api_keys[API_KEY_POINTER].use = False

            json.dump(
                api_keys,
                open(ELEVENLABS_API_KEYS_JSON, "w"),
                indent=4,
            )
            await asyncio.sleep(random.randint(10, 60))


def text_to_spans(text: str | list[str]) -> list:
    return [
        {"text": t.replace(" ", "").replace("–", "-").strip()}
        for t in (text.split() if isinstance(text, str) else text)
    ]


def generate_error_manuscript(article_id: str, scraping_url: str) -> dict:
    article_url = f"{scraping_url}/{article_id}"

    res_dir = DB_DIR / ERROR_ID
    res_dir.mkdir(parents=True, exist_ok=True)

    audio_dir = res_dir / AUDIO_DIR_NAME
    audio_dir.mkdir(parents=True, exist_ok=True)

    article = {
        "title": article_id.replace("_", " "),
        "url": None,
        "state": "error" if article_id != ERROR_ID else "done",
        # "forced_voice": "Ella",
        "sections": [
            {
                "section_type": "h1",
                "audio_path": str((audio_dir / f"{0:04}.mp3").absolute()),
                "audio_url": f'/{((audio_dir / f"{0:04}.mp3").relative_to(DB_DIR.parent))}',
                "alignment_path": str((audio_dir / f"{0:04}.json").absolute()),
                "alignment_url": f'/{((audio_dir / f"{0:04}.json").relative_to(DB_DIR.parent))}',
                "spans": text_to_spans("Error"),
            }
        ]
        + [
            {
                "section_type": "p",
                "audio_path": str((audio_dir / f"{i+1:04}.mp3").absolute()),
                "audio_url": f'/{((audio_dir / f"{i+1:04}.mp3").relative_to(DB_DIR.parent))}',
                "alignment_path": str((audio_dir / f"{i+1:04}.json").absolute()),
                "alignment_url": f'/{((audio_dir / f"{i+1:04}.json").relative_to(DB_DIR.parent))}',
                "spans": text_to_spans(s),
            }
            for i, s in enumerate(
                [
                    "The system could not process this article. Either the article does not exist, or an error occurred during the download. The system will continue to attempt to process the article, in case the problem is temporary.",
                ]
            )
        ],
        "group": "info",
        "outro": {
            "audio_path": str((audio_dir / "outro.mp3").absolute()),
            "audio_url": "/"
            + str((audio_dir / "outro.mp3").relative_to(DB_DIR.parent)),
        },
    }
    if article_id != ERROR_ID:
        article["url"] = f"{scraping_url}/{article_id}"
    return article


def generate_home_manuscript(audio_dir: pathlib.Path) -> dict:
    return {
        "title": "Empire Wikipedia Winds of Speech",
        "url": None,
        "state": "done",
        # "forced_voice": "Ella",
        "sections": [
            {
                "section_type": "h1",
                "audio_path": str((audio_dir / f"{0:04}.mp3").absolute()),
                "audio_url": "/"
                + str((audio_dir / f"{0:04}.mp3").relative_to(DB_DIR.parent)),
                "alignment_path": str((audio_dir / f"{0:04}.json").absolute()),
                "alignment_url": "/"
                + str((audio_dir / f"{0:04}.json").relative_to(DB_DIR.parent)),
                "spans": text_to_spans("Empire Wikipedia Winds of Speech"),
            },
            *[
                {
                    "section_type": "p",
                    "audio_path": str((audio_dir / f"{i+1:04}.mp3").absolute()),
                    "audio_url": "/"
                    + str((audio_dir / f"{i+1:04}.mp3").relative_to(DB_DIR.parent)),
                    "alignment_path": str((audio_dir / f"{i+1:04}.json").absolute()),
                    "alignment_url": "/"
                    + str((audio_dir / f"{i+1:04}.json").relative_to(DB_DIR.parent)),
                    "spans": text_to_spans(text),
                }
                for i, text in enumerate(
                    [
                        "This is an unofficial text-to-speech tool to help better focus on and understand the articles on the Empire Wikipedia.",
                        'It is pretty simple to use: When you find an article on the Empire Wikipedia you would like to listen to and read along with, add a "p" to the start of the URL. You\'ll then go directly to the text-to-speech article on this website (see the video clip below). If the article seems outdated, it may be because you are the first to visit it in a while, so please let the system update the article - this can take a bit.',
                        "If you would rather listen to the articles as a podcast you can find buttons for various podcast websites on the right. Please contact me if you would like more to be added.",
                    ]
                )
            ],
            {
                "section_type": "img",
                "src": "/static/img/tts.gif",
                "alt": "A video clip illustrating how to access text-to-speech directly from the Empire Wikipedia.",
                "spans": [],
            },
            *[
                {
                    "section_type": "p",
                    "audio_path": str((audio_dir / f"{i+5:04}.mp3").absolute()),
                    "audio_url": "/"
                    + str((audio_dir / f"{i+5:04}.mp3").relative_to(DB_DIR.parent)),
                    "alignment_path": str((audio_dir / f"{i+5:04}.json").absolute()),
                    "alignment_url": "/"
                    + str((audio_dir / f"{i+5:04}.json").relative_to(DB_DIR.parent)),
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
        "group": "info",
        "outro": {
            "audio_path": str((audio_dir / "outro.mp3").absolute()),
            "audio_url": "/"
            + str((audio_dir / "outro.mp3").relative_to(DB_DIR.parent)),
        },
    }


def generate_disallowed_manuscript(article_id: str, scraping_url: str) -> dict:
    article_url = f"{scraping_url}/{article_id}"

    res_dir = DB_DIR / DISALLOWED_ID
    res_dir.mkdir(parents=True, exist_ok=True)

    audio_dir = res_dir / AUDIO_DIR_NAME
    audio_dir.mkdir(parents=True, exist_ok=True)

    article = {
        "title": article_id.replace("_", " "),
        "url": None,
        "state": "disallowed" if article_id != DISALLOWED_ID else "done",
        # "forced_voice": "Ella",
        "sections": [
            {
                "section_type": "h1",
                "audio_path": str((audio_dir / f"{0:04}.mp3").absolute()),
                "audio_url": f'/{((audio_dir / f"{0:04}.mp3").relative_to(DB_DIR.parent))}',
                "alignment_path": str((audio_dir / f"{0:04}.json").absolute()),
                "alignment_url": f'/{((audio_dir / f"{0:04}.json").relative_to(DB_DIR.parent))}',
                "spans": text_to_spans("Disallowed article"),
            }
        ]
        + [
            {
                "section_type": "p",
                "audio_path": str((audio_dir / f"{i+1:04}.mp3").absolute()),
                "audio_url": f'/{((audio_dir / f"{i+1:04}.mp3").relative_to(DB_DIR.parent))}',
                "alignment_path": str((audio_dir / f"{i+1:04}.json").absolute()),
                "alignment_url": f'/{((audio_dir / f"{i+1:04}.json").relative_to(DB_DIR.parent))}',
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
        "group": "info",
        "outro": {
            "audio_path": str((audio_dir / "outro.mp3").absolute()),
            "audio_url": "/"
            + str((audio_dir / "outro.mp3").relative_to(DB_DIR.parent)),
        },
    }
    if article_id != DISALLOWED_ID:
        article["url"] = f"{scraping_url}/{article_id}"
    return article


def has_class(content: bs4.Tag, class_name: str) -> bool:
    return bool(
        content.attrs and "class" in content.attrs and class_name in content["class"]
    )


def content_to_sections(
    content: bs4.Tag, audio_dir: pathlib.Path
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
                        "audio_url": my_url(
                            "/"
                            + str(
                                (audio_dir / f"{i+1:04}.mp3").relative_to(DB_DIR.parent)
                            )
                        ),
                        "alignment_path": str(
                            (audio_dir / f"{i+1:04}.json").absolute()
                        ),
                        "alignment_url": "/"
                        + str(
                            (audio_dir / f"{i+1:04}.json").relative_to(DB_DIR.parent)
                        ),
                        "spans": text_to_spans(block),
                    }
                    i += 1
        else:
            text = child.text.strip()

        if text:
            yield {
                "section_type": child.name,
                "audio_path": str((audio_dir / f"{i+1:04}.mp3").absolute()),
                "audio_url": my_url(
                    "/" + str((audio_dir / f"{i+1:04}.mp3").relative_to(DB_DIR.parent))
                ),
                "alignment_path": str((audio_dir / f"{i+1:04}.json").absolute()),
                "alignment_url": "/"
                + str((audio_dir / f"{i+1:04}.json").relative_to(DB_DIR.parent)),
                "spans": text_to_spans(text),
            }
            i += 1


def get_api_revisions(
    article_id: str, rvlimit: int = 500, rvstart: str | None = None
) -> list:
    url = (
        f"{API_URL}?action=query&prop=revisions&format=json&titles={urllib.parse.quote(article_id)}"
        + (f"&rvlimit={rvlimit}" if rvlimit else "")
        + (f"&rvstart={urllib.parse.quote(rvstart)}" if rvstart else "")
    )
    while True:
        try:
            api_metadata_pages = list(
                httpx.get(url, timeout=60).json()["query"]["pages"].values()
            )
            break
        except httpx.ConnectTimeout as e:
            logger.warning(
                f'Could not get api revisions for acticle "{article_id}" ({url}). Retrying in 10 seconds: {e}'
            )
            time.sleep(10)

    assert len(api_metadata_pages) == 1
    return (
        list(api_metadata_pages[0]["revisions"])
        if "revisions" in api_metadata_pages[0]
        else []
    )


def generate_manuscript(
    article_id: str, scraping_url: str, res_dir: pathlib.Path, audio_dir: pathlib.Path
) -> dict:
    url = my_url(f"{scraping_url}/{article_id}")

    if any(r for r in DISALLOWED_ARTICLES if r.match(article_id)):
        logger.warning(f'"{article_id}" is disallowed')
        return generate_disallowed_manuscript(article_id, scraping_url)

    if article_id == HOME_ID:
        return generate_home_manuscript(audio_dir)

    try:
        response = httpx.get(url, verify=False, timeout=60)
    except Exception as e:
        logger.error(f'Could not get article "{url}": {e}')
        return generate_error_manuscript(article_id, scraping_url)

    if not response.is_success:
        logger.error(f'Could not get article "{url}": {response}')
        return generate_error_manuscript(article_id, scraping_url)

    soup = bs4.BeautifulSoup(response.text, "html.parser")
    content = soup.find(id="mw-content-text")

    if not isinstance(content, bs4.Tag):
        logger.error(f'Soup for "{url}" does not contain ID "mw-content-text"')
        return generate_error_manuscript(article_id, scraping_url)

    # Get image
    img_tag = content.find("img")
    img_url = f"{PD_URL}{img_tag['src']}" if isinstance(img_tag, bs4.Tag) else None

    # Get page categories
    page_categories_soup = soup.find("div", {"id": "pageCategories"})
    page_categories = (
        [a.text for a in page_categories_soup.find_all("a")]
        if isinstance(page_categories_soup, bs4.Tag)
        else []
    )

    while isinstance(content, bs4.Tag) and len(list(content.children)) == 1:
        content = list(content.children)[0]  # type: ignore

    title_tag = soup.find("h1")
    if not isinstance(title_tag, bs4.Tag):
        logger.error(f'Soup for "{url}" does not contain h1 header')
        return generate_error_manuscript(article_id, scraping_url)
    title = title_tag.text.strip()

    toc = content.find("div", {"id": "toc"})
    if isinstance(toc, bs4.Tag):
        toc.decompose()  # remove Table of Content

    for child in content.findChildren("div", recursive=False):
        if (
            not isinstance(child, bs4.Tag)
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

    revisions = get_api_revisions(article_id)
    if revisions:
        while tmp_revisions := get_api_revisions(
            article_id,
            rvstart=(
                dateutil.parser.parse(revisions[-1]["timestamp"])
                - datetime.timedelta(seconds=1)
            ).isoformat(),
        ):
            revisions += tmp_revisions
    else:
        logger.error(f'Article does not have any revisions - "{article_id}"')

    article = {
        "title": title,
        "url": url,
        "state": "generating",
        "group": scraping_url,
        "categories": page_categories,
        "outro": {
            "audio_path": str((audio_dir / "outro.mp3").absolute()),
            "audio_url": my_url(
                "/" + str((audio_dir / "outro.mp3").relative_to(DB_DIR.parent))
            ),
        },
        "lastmod": (
            dateutil.parser.parse(revisions[0]["timestamp"])
            if revisions
            else datetime.datetime.now()
        ),
        "created": (
            dateutil.parser.parse(revisions[-1]["timestamp"])
            if revisions
            else datetime.datetime.now()
        ),
        "sections": [
            {
                "section_type": "h1",
                "audio_path": str((audio_dir / f"{0:04}.mp3").absolute()),
                "audio_url": my_url(
                    "/" + str((audio_dir / f"{0:04}.mp3").relative_to(DB_DIR.parent))
                ),
                "alignment_path": str((audio_dir / f"{0:04}.json").absolute()),
                "alignment_url": "/"
                + str((audio_dir / f"{0:04}.json").relative_to(DB_DIR.parent)),
                "spans": text_to_spans(title),
            },
            *sections,
        ],
    }

    # Add image
    if img_url:
        article["img"] = img_url
    return article


def generate_complete_audio(article_id: str) -> None:
    article_id = article_id.replace(" ", "_")
    manuscript = COLLECTION.find_one({"_id": article_id})
    sound = None
    transcript = []
    if not manuscript:
        logger.warning(f'Could not find "{article_id}"')
        return
    if manuscript["state"] == "generating":
        logger.warning(
            f'Could not generate complete audio for "{article_id}" as it is still generating'
        )
        return

    logger.info(f'Generating complete audio file for "{manuscript["title"]}"')
    for section in manuscript["sections"]:
        if section["section_type"] not in SECTION_TYPE_SKIP:
            if sound:
                if section["section_type"] not in SECTION_TYPE_PRE_DELAY:
                    logger.warning(
                        f'"{section["section_type"]}" not in SECTION_TYPE_PRE_DELAY! Using default 1s'
                    )
                    sound = sound.append(
                        pydub.AudioSegment.silent(duration=1000),
                        crossfade=0,
                    )
                else:
                    sound = sound.append(
                        pydub.AudioSegment.silent(
                            duration=int(
                                SECTION_TYPE_PRE_DELAY[section["section_type"]] * 1000
                            )
                        ),
                        crossfade=0,
                    )
            else:
                sound = pydub.AudioSegment.silent(duration=0)

            transcript.append(
                {
                    "type": section["section_type"],
                    "body": " ".join(s["text"] for s in section["spans"]),
                    "startTime": len(sound) / 1000,
                }
            )
            sound = sound.append(
                pydub.AudioSegment.from_file(section["audio_path"], format="mp3"),
                crossfade=0,
            )

    if not sound:
        logger.error(f'No sections in "{article_id}"!')
        return

    if "outro" in manuscript and "audio_path" in manuscript["outro"]:
        sound = sound.append(
            pydub.AudioSegment.silent(duration=OUTRO_PRE_DELAY * 1000), crossfade=0
        )
        sound = sound.append(
            pydub.AudioSegment.from_file(
                manuscript["outro"]["audio_path"], format="mp3"
            ),
            crossfade=0,
        )
    else:
        logger.warning(
            f'"{manuscript["title"]}" has no "outro" or "outro" has no "audio_path"'
        )

    sound = sound.append(
        pydub.AudioSegment.silent(duration=OUTRO_POST_SILENCE * 1000), crossfade=0
    )

    res_dir = DB_DIR / article_id
    res_dir.mkdir(parents=True, exist_ok=True)

    audio_dir = res_dir / AUDIO_DIR_NAME
    audio_dir.mkdir(parents=True, exist_ok=True)

    audio_path = audio_dir / f"{article_id}.mp3"
    sound.export(audio_path, bitrate=f"{ELEVENLABS_BITRATE//1000}k", format="mp3")

    COLLECTION.update_one(
        {"_id": manuscript["_id"]},
        {
            "$set": {
                "complete_audio_path": str(audio_path.absolute()),
                "complete_audio_url": my_url(
                    f"/{audio_path.relative_to(DB_DIR.parent)}"
                ),
                "transcript": transcript,
            }
        },
    )
    logger.info(f'Complete audio file generated for "{manuscript["title"]}"')


def generate_audio(manuscript: dict, task: str, api_keys: list[APIKey]) -> None:
    global API_KEY_POINTER

    tmp_voice = random.choice([v for v in VOICES if v["use"]])
    if "forced_voice" in manuscript:
        v = next(
            (v for v in VOICES if v["nickname"] == manuscript["forced_voice"]), None
        )
        if v:
            tmp_voice = v
        else:
            logger.warning(
                f'Forced voice "{manuscript["forced_voice"]}" does not exist in config, please add'
            )
        logger.info(
            f'Chose forced voice "{tmp_voice["nickname"]}" for "{manuscript["title"]}"'
        )
    else:
        logger.info(
            f'Chose voice "{tmp_voice["nickname"]}" for "{manuscript["title"]}"'
        )

    voice = ELVoice(**tmp_voice)
    if isinstance(voice.id, str):
        _r = httpx.get(
            f"https://api.elevenlabs.io/v1/voices/{voice.id}",
            headers={"xi-api-key": api_keys[API_KEY_POINTER].key},
            timeout=60,
        )
        voice.name = _r.json()["name"]
    else:
        for _i in voice.id:
            _r = httpx.get(
                f"https://api.elevenlabs.io/v1/voices/{_i}",
                headers={"xi-api-key": api_keys[API_KEY_POINTER].key},
                timeout=60,
            )
            if _r.is_success:
                voice.id = _i
                voice.name = _r.json()["name"]
                break

    for i, section in enumerate(manuscript["sections"]):
        COLLECTION.update_one(
            {"_id": manuscript["_id"]},
            {"$set": {"progress": i / len(manuscript["sections"])}},
        )

        if text := " ".join(s["text"] for s in section["spans"]).strip():
            audio, alignment = asyncio.run(
                generate_voice_from_text(text, voice, api_keys)
            )
            if section["section_type"] == "ul" or section["section_type"] == "ol":
                for s in section["spans"]:
                    alignment = replace_sublist(
                        alignment, s["text"].split(), s["text"], 0, False
                    )

            audio.export(
                section["audio_path"],
                bitrate=f"{ELEVENLABS_BITRATE//1000}k",
                format="mp3",
            )
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
            f'This article was read aloud by the artificial voice, "{voice.nickname}".'
            + (
                " All content of this article is the original work of Profound Decisions and can be found on the Empire wikipedia."
                if manuscript["_id"] not in [HOME_ID, DISALLOWED_ID, ERROR_ID]
                else ""
            )
            + " Thank you for listening.",
            voice,
            api_keys,
        )
    )
    audio.export(
        manuscript["outro"]["audio_path"],
        bitrate=f"{ELEVENLABS_BITRATE//1000}k",
        format="mp3",
    )
    logger.info(f'All TTS audio segments generated for "{manuscript["title"]}"')


def insert_or_replace(manuscript: dict) -> None:
    try:
        COLLECTION.insert_one(manuscript)
    except pymongo.errors.DuplicateKeyError:
        COLLECTION.replace_one({"_id": manuscript["_id"]}, manuscript)

    META.update_one(
        {"_id": "meta"},
        {"$set": {"lastmodified": datetime.datetime.now(datetime.UTC)}},
    )


def update_manuscript(manuscript: dict, task: str = "Updating manuscript") -> None:
    generate_audio(
        manuscript,
        task,
        [APIKey(**k) for k in json.load(open(ELEVENLABS_API_KEYS_JSON))],
    )
    manuscript["state"] = "done"
    insert_or_replace(manuscript)

    generate_complete_audio(manuscript["_id"])


def get_article(article_id: str) -> typing.Any:
    article_id = article_id.replace(" ", "_")
    if not article_id:
        article_id = HOME_ID
    return COLLECTION.find_one({"_id": article_id})


def tmp_morph(section: dict) -> dict:
    section["audio_url"] = my_url(section["audio_url"])
    section["alignment_url"] = my_url(section["alignment_url"])
    return section


def article_processor(queue: multiprocessing.Queue) -> None:
    global API_KEY_POINTER
    while True:
        API_KEY_POINTER = 0
        article_id, scraping_url = queue.get(block=True, timeout=None)

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
                **generate_manuscript(article_id, scraping_url, res_dir, audio_dir),
            }

            if manuscript["state"] == "disallowed":
                manuscript["lastmod"] = datetime.datetime.now()
                insert_or_replace(manuscript)
                queue.put((DISALLOWED_ID, scraping_url))
                continue
            elif manuscript["state"] == "error":
                manuscript["lastmod"] = datetime.datetime.now()
                insert_or_replace(manuscript)
                queue.put((ERROR_ID, scraping_url))
                continue

            existing_manuscript = COLLECTION.find_one({"_id": article_id})

            # ================= TMP =================
            # existing_manuscript["sections"] = [  # type: ignore
            #     tmp_morph(s) for s in existing_manuscript["sections"]  # type: ignore
            # ]
            # existing_manuscript["outro"]["audio_url"] = my_url(  # type: ignore
            #     existing_manuscript["outro"]["audio_url"]  # type: ignore
            # )
            # existing_manuscript["complete_audio_url"] = my_url(  # type: ignore
            #     existing_manuscript["complete_audio_url"]  # type: ignore
            # )
            # logger.debug(existing_manuscript)
            # insert_or_replace(existing_manuscript)  # type: ignore
            # ================= TMP =================

            if existing_manuscript is not None:
                # if "forced_voice" in existing_manuscript:
                #     manuscript["forced_voice"] = existing_manuscript["forced_voice"]

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
            and (
                "complete_audio_url" not in a
                or "transcript" not in a
                or "complete_audio_path" not in a
                or not pathlib.Path(a["complete_audio_path"]).exists()
            )
        ):
            try:
                generate_complete_audio(a["_id"])
            except Exception as e:
                logger.error(
                    f'Article "{a["title"]}" has breaking errors, force-updating manuscript - "{type(e)}: {e}"'
                )
                update_manuscript(a, "Manuscript error")
                generate_complete_audio(a["_id"])


article_queue: multiprocessing.Queue = multiprocessing.Queue()
multiprocessing.Process(target=article_processor, args=(article_queue,)).start()


@APP.get("/sitemap.xml")
def sitemap() -> fastapi.Response:
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
            if "created" in manuscript:
                sitemap += (
                    f"\n		<created>{manuscript["created"].date().isoformat()}</created>"
                )
            sitemap += f"\n		<changefreq>monthly</changefreq>"
            sitemap += "\n	</url>"
            sitemap += "\n"
    sitemap += "\n</urlset>"
    sitemap += "\n"

    return fastapi.Response(content=sitemap, media_type="application/xml")


# @app.get("/")
# def home() -> starlette.responses.FileResponse:
#     return starlette.responses.FileResponse(WEB_DIR / "index.html")


@APP.get("/api/manuscript/{article_id:path}")
def manuscript(article_id: str, scraping_url: str = WIKI_URL) -> typing.Any:
    manuscript = get_article(article_id)
    article_queue.put((article_id, scraping_url))
    if manuscript is not None:
        return manuscript
    else:
        insert_or_replace(
            {
                "_id": article_id,
                "progress": 0.0,
                "title": article_id,
                "url": f"{scraping_url}/{article_id}",
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
            "url": f"{scraping_url}/{article_id}",
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


@APP.get("/api/complete_audio/{article_id:path}")
def complete_audio(article_id: str) -> str:
    manuscript = get_article(article_id)
    if not isinstance(manuscript, dict) or manuscript["state"] != "done":
        raise Exception("Article not generated")
    if (
        "complete_audio_url" not in manuscript
        or "complete_audio_path" not in manuscript
        or not pathlib.Path(manuscript["complete_audio_path"]).exists()
    ):
        logger.debug(pathlib.Path(manuscript["complete_audio_path"]))
        generate_complete_audio(article_id)

    manuscript = get_article(article_id)
    assert isinstance(manuscript, dict)
    return str(manuscript["complete_audio_url"])


APP.mount(
    "/static/",
    fastapi.staticfiles.StaticFiles(directory=WEB_DIR, html=True),
    name="Web",
)
APP.mount("/robots.txt", fastapi.staticfiles)

APP.mount("/db/", fastapi.staticfiles.StaticFiles(directory=DB_DIR), name="DB")


@APP.get("/empire-wiki/{article_id:path}")
@APP.get("/{article_id:path}")
def index(article_id: str) -> fastapi.responses.HTMLResponse:
    with open(WEB_DIR / "index.html") as f:
        index = f.read()
    article = get_article(article_id)
    logger.debug(article_id)
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

    return fastapi.responses.HTMLResponse(
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
