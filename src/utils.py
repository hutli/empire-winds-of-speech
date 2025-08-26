import os
import pathlib

DB_DIR = pathlib.Path(os.environ["DB_DIR"])


def url_to_path(url: str) -> pathlib.Path:
    url = url.replace("%3F", "?")
    if url.startswith("/db/"):
        return DB_DIR / url.replace("/db/", "", 1)
    else:
        raise Exception(f'Do not know how to convert url "{url}" to path')
