"""Settings keys that moved or left between versions: read under the old name, written under
the new one once, or read and dropped. `RENAMES` and `RETIRED` are the one homes of the two
lists; each name is dotted for a key inside a section."""

RENAMES: list[tuple[str, str]] = []

# The Low Quota switch, whose value can carry a choice of mode (`_mode_chosen`). The entry and
# that rule go together, when no settings file in use still holds the key: every app install
# has started once on a core that drops it (its `init` rewrites the file), and every
# hand-written server file that holds it has been edited. Removed earlier, the key would only
# be listed as unknown: a file with it on and no `routing` would fall to `relay` and cut every
# request, and a copy of the old example would follow Auto's rule and pass every request through.
RETIRED: tuple[str, ...] = ("low_quota.enabled",)

# `config._flag`'s truth set: a typo is not true, and never refuses the file.
_TRUE = ("1", "true", "yes", "on")


def _get(doc, dotted):
    section, _, key = dotted.rpartition(".")
    holder = doc.get(section) if section else doc
    if not isinstance(holder, dict) or key not in holder:
        return None, None
    return holder, key


def _put(doc, dotted, value):
    section, _, key = dotted.rpartition(".")
    if section:
        holder = doc.get(section)
        if not isinstance(holder, dict):
            holder = doc[section] = {}
    else:
        holder = doc
    holder[key] = value


def _drop_if_empty(doc, dotted):
    section, _, _ = dotted.rpartition(".")
    if section and isinstance(doc.get(section), dict) and not doc[section]:
        del doc[section]


def _text(value):
    """As the loader reads a value: unset and blank are the same."""
    return str(value).strip() if value is not None else ""


def _true(value):
    if isinstance(value, bool):
        return value
    return _text(value).lower() in _TRUE


def _mode_chosen(doc, enabled):
    """The mode a document chose in the retired switch, or None when it chose none there.
    On with no `routing`: Auto under the old default. Off under `auto` with no secret file:
    every request was cut and no quota could be read, which is what a copy of the old
    example is."""
    routing = _text(doc.get("routing"))
    if _true(enabled):
        return None if routing else "auto"
    cliproxy = doc.get("cliproxy")
    secret = _text(cliproxy.get("management_secret_file")) if isinstance(cliproxy, dict) else ""
    return "relay" if routing == "auto" and not secret else None


def migrate(doc, renames=None):
    """Move each value found under an old name to its new name, unless the new name is
    already set (then the old one is dropped), then drop each retired key, setting the
    `routing` it chose. Returns what it applied, in order, as (old, new) pairs: a retired
    key's `new` is "routing: <mode>" when it set one, else None. `renames` defaults to the
    module's table at call time, so a test may patch `RENAMES`."""
    applied = []
    for old, new in (RENAMES if renames is None else renames):
        holder, key = _get(doc, old)
        if holder is None:
            continue
        value = holder.pop(key)
        new_holder, _ = _get(doc, new)
        if new_holder is None:
            _put(doc, new, value)
        _drop_if_empty(doc, old)
        applied.append((old, new))
    for old in RETIRED:
        holder, key = _get(doc, old)
        if holder is None:
            continue
        value = holder.pop(key)
        mode = _mode_chosen(doc, value)
        if mode:
            doc["routing"] = mode
        _drop_if_empty(doc, old)
        applied.append((old, f"routing: {mode}" if mode else None))
    return applied


def said(pair):
    """An applied pair as `init` notes it."""
    old, new = pair
    return f"{old} -> {new if new is not None else '(dropped)'}"
