import datetime
import json
import os
import pathlib
import typing

import fastapi
import fastapi.staticfiles
import loguru
import markdownify
import mutagen
import mutagen.easyid3
import mutagen.id3
import mutagen.mp3
import pod2gen
import pymongo
import tqdm

NAME = os.environ["NAME"]
DESCRIPTION = os.environ["DESCRIPTION"]
CATEGORY = pod2gen.Category(*json.loads(os.environ["CATEGORY"].strip('"')))
LANGUAGE = os.environ["LANGUAGE"]
OWNER = pod2gen.Person(**json.loads(os.environ["OWNER"].strip('"')))
AUTHOR = pod2gen.Person(**json.loads(os.environ["AUTHOR"].strip('"')))
PERSONS = [AUTHOR, OWNER]
URL = os.environ["URL"]
EPISODE_URL = os.environ["EPISODE_URL"]
WEB = os.environ["WEB"]
ART = os.environ["ART"]
EPISODE_LINK_BASE = os.environ["EPISODE_LINK_BASE"]
MONGODB_DOMAIN = os.environ["MONGODB_DOMAIN"]
WEB_DIR = pathlib.Path("/app/web")
DB_DIR = WEB_DIR / "db"
MANUSCRIPT_FILTER_GROUP = os.environ["MANUSCRIPT_FILTER_GROUP"]
MANUSCRIPT_FILTER_CATEGORY = os.environ.get("MANUSCRIPT_FILTER_CATEGORY", None)
MANUSCRIPT_FULL_TYPE_CATEGORY = os.environ.get("MANUSCRIPT_FULL_TYPE_CATEGORY", None)
CHAPTER_SEGMENT_TYPE = os.environ["CHAPTER_SEGMENT_TYPE"]

METADATA_DIR = pathlib.Path("metadata")
METADATA_DIR.mkdir(parents=True, exist_ok=True)

app = fastapi.FastAPI()
mongodb_client: pymongo.MongoClient = pymongo.MongoClient(MONGODB_DOMAIN, 27017)
DB = mongodb_client["database"]
COLLECTION = DB["manuscripts"]
META = DB["meta"]


def get_episode(manuscript: dict) -> typing.Any:
    manuscript_id = manuscript["_id"].replace("?", "%3F")
    try:
        return pod2gen.Episode(
            title=manuscript["title"],
            summary=next(
                (
                    " ".join(span["text"] for span in section["spans"]).strip()
                    for section in manuscript["sections"]
                    if section["section_type"] == "p"
                ),
                None,
            ),
            long_summary=pod2gen.htmlencode(
                markdownify.markdownify(
                    "".join(
                        f"<{section["section_type"]}>{" ".join(span["text"] for span in section["spans"]).strip()}</{section["section_type"]}>"
                        for section in manuscript["sections"]
                    )
                )
            ),
            media=pod2gen.Media(
                f"{EPISODE_URL}/audio/{manuscript_id}.mp3",
                size=os.stat(DB_DIR / manuscript["complete_audio_path"]).st_size,
                duration=datetime.timedelta(
                    seconds=mutagen.File(
                        DB_DIR / manuscript["complete_audio_path"]
                    ).info.length
                ),
            ),
            persons=PERSONS,
            authors=PERSONS,
            publication_date=(
                manuscript["created"]
                if "created" in manuscript
                else manuscript["lastmod"]
            ).replace(tzinfo=datetime.timezone.utc),
            explicit=True,
            image=manuscript["img"] if "img" in manuscript else None,
            link=f"{EPISODE_LINK_BASE}{manuscript_id}",
            episode_type=(
                pod2gen.EPISODE_TYPE_FULL
                if "categories" not in manuscript
                or MANUSCRIPT_FULL_TYPE_CATEGORY
                and MANUSCRIPT_FULL_TYPE_CATEGORY.lower()
                in [c.lower() for c in manuscript["categories"]]
                else pod2gen.EPISODE_TYPE_BONUS
            ),
            chapters_json=f"{EPISODE_URL}/chapters_json/{manuscript_id}.json",
            transcripts=[
                pod2gen.Transcript(
                    f"{EPISODE_URL}/transcript/{manuscript_id}.srt",
                    "application/srt",
                    language="en-GB",
                    is_caption=True,
                )
            ],
        )
    except mutagen.mp3.HeaderNotFoundError as e:
        loguru.logger.error(f"{manuscript_id}: {e}")
        return None


lastmodified = datetime.datetime.min
podcast = None


@app.head("/")
@app.get("/")
def index() -> fastapi.Response:
    global lastmodified
    global podcast

    meta = META.find_one({"_id": "meta"})
    if meta:
        _lastmodified = meta["lastmodified"]
        if isinstance(_lastmodified, datetime.datetime):
            if _lastmodified > lastmodified:
                loguru.logger.info(
                    f"Manuscript updated ({_lastmodified} > {lastmodified}) - regenerating podcast"
                )
                lastmodified = _lastmodified
                manuscripts = list(COLLECTION.find())
                _podcast = pod2gen.Podcast(
                    name=NAME,
                    description=DESCRIPTION,
                    persons=PERSONS,
                    authors=PERSONS,
                    owner=OWNER,
                    category=CATEGORY,
                    website=WEB,
                    image=ART,
                    explicit=True,
                    language=LANGUAGE,
                    feed_url=URL,
                )
                _podcast.episodes += [
                    get_episode(manuscript)
                    for manuscript in tqdm.tqdm(
                        sorted(manuscripts, key=lambda a: a["_id"]),
                        total=len(manuscripts),
                    )
                    if manuscript["state"] == "done"
                    and "complete_audio_url" in manuscript
                    and "complete_audio_path" in manuscript
                    and (
                        "group" not in manuscript
                        or manuscript["group"] == MANUSCRIPT_FILTER_GROUP
                    )
                    and (
                        not MANUSCRIPT_FILTER_CATEGORY
                        or (
                            "categories" in manuscript
                            and MANUSCRIPT_FILTER_CATEGORY.lower()
                            in [c.lower() for c in manuscript["categories"]]
                        )
                    )
                ]
                _podcast.episodes = [e for e in _podcast.episodes if e]
                podcast = _podcast.rss_str()
        else:
            loguru.logger.error(
                f'Meta "lastmodified" have incorrect correct type, got {type(_lastmodified)} expected {datetime.datetime}. Cannot update smartly...'
            )
    else:
        loguru.logger.error(f"Meta entry does not exist! Cannot update smartly...")

    return fastapi.Response(content=podcast, media_type="application/rss+xml")


def iterfile(path: pathlib.Path) -> typing.Generator[bytes, None, None]:
    with open(path, mode="rb") as file_like:
        yield from file_like


def send_bytes_range_requests(
    file_obj: typing.BinaryIO, start: int, end: int, chunk_size: int = 10_000
) -> typing.Generator[bytes, None, None]:
    """Send a file in chunks using Range Requests specification RFC7233

    `start` and `end` parameters are inclusive due to specification
    """
    with file_obj as f:
        f.seek(start)
        while (pos := f.tell()) <= end:
            read_size = min(chunk_size, end + 1 - pos)
            yield f.read(read_size)


def _get_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    def _invalid_range() -> fastapi.HTTPException:
        return fastapi.HTTPException(
            fastapi.status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail=f"Invalid request range (Range:{range_header!r})",
        )

    try:
        h = range_header.replace("bytes=", "").split("-")
        start = int(h[0]) if h[0] != "" else 0
        end = int(h[1]) if h[1] != "" else file_size - 1
    except ValueError:
        raise _invalid_range()

    if start > end or start < 0 or end > file_size - 1:
        raise _invalid_range()
    return start, end


def range_requests_response(
    request: fastapi.Request, file_path: str, content_type: str
) -> fastapi.responses.StreamingResponse:
    """Returns StreamingResponse using Range Requests of a given file"""

    file_size = os.stat(file_path).st_size
    range_header = request.headers.get("range")

    headers = {
        "content-type": content_type,
        "accept-ranges": "bytes",
        "content-encoding": "identity",
        "content-length": str(file_size),
        "access-control-expose-headers": (
            "content-type, accept-ranges, content-length, "
            "content-range, content-encoding"
        ),
    }
    start = 0
    end = file_size - 1
    status_code = fastapi.status.HTTP_200_OK

    if range_header is not None:
        start, end = _get_range_header(range_header, file_size)
        size = end - start + 1
        headers["content-length"] = str(size)
        headers["content-range"] = f"bytes {start}-{end}/{file_size}"
        status_code = fastapi.status.HTTP_206_PARTIAL_CONTENT

    return fastapi.responses.StreamingResponse(
        send_bytes_range_requests(open(file_path, mode="rb"), start, end),
        headers=headers,
        status_code=status_code,
    )


def get_manuscript(episode_id: str) -> typing.Any:
    manuscript = COLLECTION.find_one({"_id": episode_id})
    if not manuscript:
        raise fastapi.HTTPException(
            detail=f'Episode "{episode_id}" does not exist',
            status_code=fastapi.status.HTTP_404_NOT_FOUND,
        )

    if "transcript" not in manuscript:
        raise fastapi.HTTPException(
            detail=f'Episode "{episode_id}" has no transcript generated yet',
            status_code=fastapi.status.HTTP_404_NOT_FOUND,
        )

    return manuscript


@app.head("/audio/{episode_id}.mp3")
@app.get("/audio/{episode_id}.mp3")
def audio(req: fastapi.Request, episode_id: str) -> fastapi.Response:
    manuscript = get_manuscript(episode_id)

    audio_file = DB_DIR / manuscript["complete_audio_path"]

    try:
        easyid_file = mutagen.easyid3.EasyID3(audio_file)
        easyid_file["title"] = manuscript["title"]
        easyid_file["album"] = NAME
        easyid_file["artist"] = ",".join([p.name for p in PERSONS])
        easyid_file.save()

        id3_file = mutagen.id3.ID3(audio_file)
        chapters = [
            c for c in manuscript["transcript"] if c["type"] == CHAPTER_SEGMENT_TYPE
        ]
        id3_file.add(
            mutagen.id3.CTOC(
                element_id="toc",
                flags=mutagen.id3.CTOCFlags.TOP_LEVEL | mutagen.id3.CTOCFlags.ORDERED,
                child_element_ids=[c["body"] for c in chapters],
                sub_frames=[
                    mutagen.id3.TIT2(text=["TOC"]),
                ],
            )
        )

        for i, chapter in enumerate(chapters):
            id3_file.add(
                mutagen.id3.CHAP(
                    element_id=chapter["body"],
                    start_time=int(chapter["startTime"] * 1000),
                    end_time=int(
                        (
                            chapters[i + 1]["startTime"]
                            if i < len(chapters) - 1
                            else mutagen.File(audio_file).info.length
                        )
                        * 1000
                    ),
                    sub_frames=[
                        mutagen.id3.TIT2(text=[chapter["body"]]),
                    ],
                )
            )
        id3_file.save()
    except UnicodeEncodeError as e:
        loguru.logger.error(f'Could not encode "{episode_id}": {e}')

    return range_requests_response(req, audio_file, "audio/mp3")


@app.head("/chapters_json/{episode_id}.json")
@app.get("/chapters_json/{episode_id}.json")
def chapters_json(episode_id: str) -> fastapi.Response:
    manuscript = get_manuscript(episode_id)

    return fastapi.responses.JSONResponse(
        content={
            "title": manuscript["title"],
            "podcastName": NAME,
            "transcript": [
                {
                    "title": s["body"],
                    "startTime": s["startTime"],
                    "endTime": int(
                        (
                            manuscript["transcript"][i + 1]["startTime"]
                            if i < len(s) - 1
                            else mutagen.File(
                                DB_DIR / manuscript["complete_audio_path"]
                            ).info.length
                        )
                        * 1000
                    ),
                }
                for i, s in enumerate(manuscript["transcript"])
                if s["type"] == CHAPTER_SEGMENT_TYPE
            ],
        }
    )


@app.head("/transcript/{episode_id}.json")
@app.get("/transcript/{episode_id}.json")
def transcript_json(episode_id: str) -> fastapi.Response:
    return fastapi.responses.JSONResponse(
        content={"segments": get_manuscript(episode_id)["transcript"]}
    )


def srt_timestamp(t: datetime.time) -> str:
    return "{:02d}:{:02d}:{:02d},{:03d}".format(
        t.hour, t.minute, t.second, t.microsecond // 1000
    )


@app.head("/transcript/{episode_id}.srt")
@app.get("/transcript/{episode_id}.srt")
def transcript_srt(episode_id: str) -> fastapi.Response:
    manuscript = get_manuscript(episode_id)

    srt = ""
    start_time = datetime.time()
    for i, segment in enumerate(manuscript["transcript"]):
        start_time = datetime.datetime.fromtimestamp(segment["startTime"]).time()
        end_time = datetime.datetime.fromtimestamp(
            manuscript["transcript"][i + 1]["startTime"]
            if i < len(manuscript["transcript"]) - 1
            else mutagen.File(DB_DIR / manuscript["complete_audio_path"]).info.length
        ).time()
        srt += f"""{i+1}
{srt_timestamp (start_time)} --> {srt_timestamp(end_time)}
{segment["body"]}

"""
    return fastapi.Response(content=srt, media_type="application/srt")


app.mount(
    "/", fastapi.staticfiles.StaticFiles(directory=WEB_DIR, html=True), name="Web"
)
