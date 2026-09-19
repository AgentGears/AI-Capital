from pathlib import Path

path = Path("src/ai_capital/product/rooted_io.py")
source = path.read_text()

old = """    descriptor: int | None = None
    temporary: str | None = None
"""
new = """    descriptor: int | None = None
    original_descriptor: int | None = None
    temporary: str | None = None
"""
if old not in source:
    raise SystemExit("descriptor anchor not found")
source = source.replace(old, new, 1)

old = """        expected_identity = _stat_identity(current)
        temporary = f\".{name}.{secrets.token_hex(12)}.tmp\"
"""
new = """        expected_identity = _stat_identity(current)
        original_descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, \"O_NONBLOCK\", 0)
            | getattr(os, \"O_BINARY\", 0),
            dir_fd=parent_fd,
        )
        original_opened = os.fstat(original_descriptor)
        if (
            not stat.S_ISREG(original_opened.st_mode)
            or _stat_identity(original_opened) != expected_identity
            or original_opened.st_nlink != 1
        ):
            raise ExecutionFailure(
                \"capability target changed before rooted materialization\"
            )
        temporary = f\".{name}.{secrets.token_hex(12)}.tmp\"
"""
if old not in source:
    raise SystemExit("expected identity anchor not found")
source = source.replace(old, new, 1)

old = """        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
"""
new = """        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if original_descriptor is not None:
            try:
                os.close(original_descriptor)
            except OSError:
                pass
"""
if old not in source:
    raise SystemExit("cleanup anchor not found")
source = source.replace(old, new, 1)

path.write_text(source)
