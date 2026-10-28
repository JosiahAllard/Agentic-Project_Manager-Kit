"""kitlib.hubsite.skilledits: the checks of a skill edit, the owner's new text for one kit skill's
source (content/skills/<name>/SKILL.md), which an agent commits (docs/action-hub.md "Skill edits").

  MAX_BYTES                  the most UTF-8 bytes an edit's text may hold
  git_blob(data)             the git blob id of bytes `data`: what `git hash-object` gives
  is_blob(v)                 whether v is a blob id (40 lowercase hex digits)
  normal(text)               text with CRLF and a lone CR read as LF (the kit's files are LF)
  front_name(text)           the `name:` of text's front matter, or None when it has none
  why_not(text, current, folder)
                             why `text` cannot replace the skill source `current` (str or None)
                             of folder `folder`, or None when it can
  source_path(name)          the skill's source path in the kit repository, with / separators
  BRANCH                     the shape of a branch name the done route takes

Nothing here reads a file, runs git, prints or exits.
"""
import hashlib
import re

MAX_BYTES = 65536
BRANCH = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$')
_BLOB = re.compile(r'^[0-9a-f]{40}$')
_FM = re.compile(r'\A---\n(.*?)\n---(?=\n|\Z)', re.S)


def git_blob(data):
    """The git blob id of bytes `data` (sha1 of the blob header and the bytes)."""
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def is_blob(v):
    return isinstance(v, str) and bool(_BLOB.match(v))


def normal(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def front_name(text):
    """The `name:` value of the front matter at the top of `text` (LF lines), or None."""
    m = _FM.match(text) if isinstance(text, str) else None
    if not m:
        return None
    for line in m.group(1).split('\n'):
        k, sep, v = line.partition(':')
        if sep and not line.startswith((' ', '\t')) and k.strip() == 'name':
            return v.strip() or None
    return None


def why_not(text, current, folder):
    """Why `text` cannot be the new SKILL.md of the skill in folder `folder` whose source now is
    `current` (None when it cannot be read), or None: text that is not text or is blank, more than
    MAX_BYTES of UTF-8, a NUL, no front matter `name:`, or a name that is not the source's (the
    folder's when the source names none)."""
    if not isinstance(text, str) or not text.strip():
        return 'the text is empty'
    try:
        size = len(text.encode('utf-8'))
    except UnicodeEncodeError:
        return 'the text is not valid Unicode'
    if size > MAX_BYTES:
        return 'the text is %d bytes of UTF-8, more than the %d a skill edit may hold' % (size, MAX_BYTES)
    if '\0' in text:
        return 'the text holds a NUL character'
    name = front_name(normal(text))
    if name is None:
        return 'the text has no front matter naming the skill (a --- block with name: at the top)'
    want = front_name(normal(current)) if isinstance(current, str) else None
    want = want or folder
    if name != want:
        return 'the front matter names %r, not %r: an edit cannot rename a skill' % (name, want)
    return None


def source_path(name):
    return 'content/skills/%s/SKILL.md' % name
