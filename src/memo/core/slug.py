import re

# 小文字英数字とハイフン。ファイル名・URL にそのまま使える形に限定する。
SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class InvalidSlug(ValueError):
    pass


def validate_slug(slug: str) -> str:
    if not SLUG_PATTERN.fullmatch(slug):
        raise InvalidSlug(
            f"invalid slug {slug!r}: use 1-64 chars of a-z, 0-9 and '-' "
            "(no leading/trailing '-')"
        )
    return slug
