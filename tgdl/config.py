import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    api_id: int
    api_hash: str
    phone: str | None
    session: str
    output_dir: Path

    @classmethod
    def load(cls) -> "Config":
        api_id = os.getenv("TG_API_ID")
        api_hash = os.getenv("TG_API_HASH")

        if not api_id or not api_hash:
            raise RuntimeError(
                "Missing TG_API_ID / TG_API_HASH. "
                "Create a .env file (see .env.example) with credentials from https://my.telegram.org"
            )

        return cls(
            api_id=int(api_id),
            api_hash=api_hash,
            phone=os.getenv("TG_PHONE") or None,
            session=os.getenv("TG_SESSION", "tgdl"),
            output_dir=Path(os.getenv("TG_OUTPUT_DIR", "downloads")),
        )
