"""A small YAML subset parser.

No PyYAML in this environment, and the graph format needs only a narrow corner
of YAML: block mappings, block sequences, inline lists/maps, quoted and plain
scalars, comments. Supporting exactly that keeps parse errors explainable and
avoids a dependency whose behaviour would also have to be emulated.

Supported:
  key: value            block mapping
  key:                  nested mapping or sequence follows
  - item                block sequence
  - key: value          sequence of mappings
  [a, b]  {k: v}        inline collections
  "x"  'x'  123  true  null  ~   flow scalars
  # comment             to end of line (outside quotes, at a token start)

Not supported, and reported rather than guessed: anchors, aliases, tags, block
scalars (| and >), multiple documents, flow collections spanning lines.
"""

from __future__ import annotations

from dataclasses import dataclass


class YamlError(ValueError):
    def __init__(self, message: str, line_no: int) -> None:
        self.line_no = line_no
        super().__init__(f"line {line_no}: {message}")


@dataclass(slots=True)
class _Line:
    indent: int
    text: str
    no: int


def _strip_comment(raw: str) -> str:
    out = []
    quote = ""
    prev = " "
    for ch in raw:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            prev = ch
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and prev in " \t":
            break
        else:
            out.append(ch)
        prev = ch
    return "".join(out).rstrip()


def _scalar(token: str, line_no: int = 0) -> object:
    text = token.strip()
    if not text:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if (text.startswith("[") and not text.endswith("]")) or (
        text.startswith("{") and not text.endswith("}")
    ):
        # Treating `[read` as the string "[read" turns a typo into a silent value
        # change; the format promises "reported rather than guessed".
        raise YamlError(f"unterminated flow collection: {text!r}", line_no)
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(part) for part in _split_inline(text[1:-1])]
    if text.startswith("{") and text.endswith("}"):
        result: dict[str, object] = {}
        for part in _split_inline(text[1:-1]):
            if ":" not in part:
                raise YamlError(f"inline mapping entry without ':': {part!r}", 0)
            key, _, value = part.partition(":")
            result[str(_scalar(key))] = _scalar(value)
        return result
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if text.lower() in ("null", "~"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _split_inline(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote = ""
    current: list[str] = []
    for ch in body:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch in "[{":
            depth += 1
            current.append(ch)
        elif ch in "]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _split_key(text: str) -> tuple[str, str] | None:
    depth = 0
    quote = ""
    for idx, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            after = text[idx + 1 : idx + 2]
            if after in ("", " ", "\t"):
                return text[:idx].strip(), text[idx + 1 :].strip()
    return None


def parse(text: str) -> object:
    lines: list[_Line] = []
    documents: list[int] = []
    for no, raw in enumerate(text.splitlines(), start=1):
        cleaned = _strip_comment(raw)
        if not cleaned.strip():
            continue
        if cleaned.strip() == "---":
            documents.append(no)
            continue
        if cleaned.strip() in ("|", ">", "|-", ">-"):
            raise YamlError("block scalars are not supported", no)
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        if "\t" in cleaned[:indent]:
            raise YamlError("tabs are not allowed for indentation", no)
        lines.append(_Line(indent, cleaned.strip(), no))
    # A leading `---` is a document marker and is harmless. A second one starts a
    # document this parser does not support: silently treating the file as one
    # document would apply only the first half of what was written.
    if len(documents) > 1:
        raise YamlError(
            "multiple YAML documents (`---`) are not supported; split the file", documents[1]
        )
    if not lines:
        return None
    value, idx = _parse_block(lines, 0, lines[0].indent)
    if idx != len(lines):
        raise YamlError("unexpected content after the document root", lines[idx].no)
    return value


def _parse_block(lines: list[_Line], idx: int, indent: int) -> tuple[object, int]:
    if idx >= len(lines):
        return None, idx
    if lines[idx].text.startswith("- "):
        return _parse_sequence(lines, idx, indent)
    if lines[idx].text == "-":
        return _parse_sequence(lines, idx, indent)
    return _parse_mapping(lines, idx, indent)


def _parse_sequence(lines: list[_Line], idx: int, indent: int) -> tuple[list, int]:
    items: list = []
    while idx < len(lines):
        line = lines[idx]
        if line.indent != indent or not (line.text.startswith("- ") or line.text == "-"):
            break
        rest = line.text[1:].strip() if line.text != "-" else ""
        if not rest:
            idx += 1
            if idx < len(lines) and lines[idx].indent > indent:
                value, idx = _parse_block(lines, idx, lines[idx].indent)
            else:
                value = None
            items.append(value)
            continue
        split = _split_key(rest)
        if split is None:
            items.append(_scalar(rest, line.no))
            idx += 1
            continue
        # `- key: value` starts a mapping whose remaining keys are indented to
        # the column where `key` starts.
        key_indent = indent + (len(line.text) - len(rest))
        body = [(line.no, rest)] + [
            (l.no, l.text) for l in lines[idx + 1 :] if l.indent == key_indent
        ]
        mapping: dict[str, object] = {}
        consumed = idx + 1
        pending_key, pending_value = split
        if pending_value:
            _put(mapping, _scalar(pending_key, line.no), _scalar(pending_value, line.no), line.no)
        else:
            if consumed < len(lines) and lines[consumed].indent > key_indent:
                value, consumed = _parse_block(lines, consumed, lines[consumed].indent)
                _put(mapping, _scalar(pending_key, line.no), value, line.no)
            else:
                _put(mapping, _scalar(pending_key, line.no), None, line.no)
        while consumed < len(lines) and lines[consumed].indent == key_indent:
            sub = lines[consumed]
            pair = _split_key(sub.text)
            if pair is None:
                raise YamlError(f"expected 'key: value', got {sub.text!r}", sub.no)
            sub_key, sub_value = pair
            consumed += 1
            if sub_value:
                _put(mapping, _scalar(sub_key, sub.no), _scalar(sub_value, sub.no), sub.no)
            else:
                if consumed < len(lines) and lines[consumed].indent > key_indent:
                    value, consumed = _parse_block(lines, consumed, lines[consumed].indent)
                    _put(mapping, _scalar(sub_key, sub.no), value, sub.no)
                else:
                    _put(mapping, _scalar(sub_key, sub.no), None, sub.no)
        del body
        items.append(mapping)
        idx = consumed
    return items, idx


def _put(mapping: dict, key: object, value: object, line_no: int) -> None:
    """Duplicate keys are a typo, not an override: the format says 'reported, not
    guessed', and silent last-wins can flip an effect list without a trace."""
    name = str(key)
    if name in mapping:
        raise YamlError(f"duplicate key {name!r}", line_no)
    mapping[name] = value


def _parse_mapping(lines: list[_Line], idx: int, indent: int) -> tuple[dict, int]:
    mapping: dict[str, object] = {}
    while idx < len(lines):
        line = lines[idx]
        if line.indent != indent:
            if line.indent > indent:
                raise YamlError("unexpected indentation", line.no)
            break
        pair = _split_key(line.text)
        if pair is None:
            raise YamlError(f"expected 'key: value', got {line.text!r}", line.no)
        key, value_token = pair
        idx += 1
        if value_token:
            _put(mapping, _scalar(key, line.no), _scalar(value_token, line.no), line.no)
            continue
        if idx < len(lines) and (
            lines[idx].indent > indent
            or (lines[idx].indent == indent and lines[idx].text.startswith("- "))
        ):
            nested, idx = _parse_block(lines, idx, lines[idx].indent)
            _put(mapping, _scalar(key, line.no), nested, line.no)
        else:
            mapping[str(_scalar(key))] = None
    return mapping, idx


# ---------------------------------------------------------------------------
# Serialisation


def dump(value: object, indent: int = 0) -> str:
    """Serialise to the same subset. Deterministic: mapping keys are sorted."""
    lines: list[str] = []
    _dump(value, indent, lines)
    return "\n".join(lines) + "\n"


def _dump(value: object, indent: int, lines: list[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for key in sorted(value):
            item = value[key]
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                _dump(item, indent + 2, lines)
            else:
                lines.append(f"{pad}{key}: {_scalar_out(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item:
                keys = sorted(item)
                first = keys[0]
                head = item[first]
                if isinstance(head, (dict, list)) and head:
                    lines.append(f"{pad}- {first}:")
                    _dump(head, indent + 4, lines)
                else:
                    lines.append(f"{pad}- {first}: {_scalar_out(head)}")
                for key in keys[1:]:
                    rest = item[key]
                    if isinstance(rest, (dict, list)) and rest:
                        lines.append(f"{pad}  {key}:")
                        _dump(rest, indent + 4, lines)
                    else:
                        lines.append(f"{pad}  {key}: {_scalar_out(rest)}")
            else:
                lines.append(f"{pad}- {_scalar_out(item)}")


def _scalar_out(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_scalar_out(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_scalar_out(v)}" for k, v in sorted(value.items())) + "}"
    text = str(value)
    if text == "" or any(ch in text for ch in ":#[]{}\"'\n") or text.strip() != text:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text
