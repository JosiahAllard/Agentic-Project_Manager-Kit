"""git's xdiff, ported: the Myers diff with its preparation (xprepare.c), the split heuristics
(xdiffi.c xdl_split), change compaction with the indent heuristic (xdl_change_compact), the hunk
emitter for -U<n> (xemit.c, default function-name lines) and the three-way merge (xmerge.c,
level ZEALOUS, style "merge"), so line numbers, hunk boundaries and conflict blocks come out as
git's do. Lines are bytes including their trailing LF; flags (whitespace modes) are not supported.
"""

XDL_MAX_EQLIMIT = 1024
XDL_SIMSCAN_WINDOW = 100
XDL_KPDIS_RUN = 4
XDL_MAX_COST_MIN = 256
XDL_HEUR_MIN_COST = 256
XDL_SNAKE_CNT = 20
XDL_K_HEUR = 4
XDL_LINE_MAX = (1 << 62)

MAX_INDENT = 200
MAX_BLANKS = 20
START_OF_FILE_PENALTY = 1
END_OF_FILE_PENALTY = 21
TOTAL_BLANK_WEIGHT = -30
POST_BLANK_WEIGHT = 6
RELATIVE_INDENT_PENALTY = -4
RELATIVE_INDENT_WITH_BLANK_PENALTY = 10
RELATIVE_OUTDENT_PENALTY = 24
RELATIVE_OUTDENT_WITH_BLANK_PENALTY = 17
RELATIVE_DEDENT_PENALTY = 23
RELATIVE_DEDENT_WITH_BLANK_PENALTY = 17
INDENT_WEIGHT = 60
INDENT_HEURISTIC_MAX_SLIDING = 100


def split_lines(data):
    """Records as xdiff sees them: each line with its LF; a last line without one stays."""
    if not data:
        return []
    out = data.split(b'\n')
    last = out.pop()
    out = [x + b'\n' for x in out]
    if last:
        out.append(last)
    return out


def _bogosqrt(n):
    i = 1
    while n > 0:
        i <<= 1
        n >>= 2
    return i


class XFile:
    __slots__ = ('recs', 'ha', 'rchg', 'nrec')

    def __init__(self, recs, ha):
        self.recs = recs
        self.ha = ha
        self.nrec = len(recs)
        # rchg[i + 1] is line i; rchg[0] and rchg[nrec + 1] are the sentinels
        self.rchg = [0] * (self.nrec + 2)

    def chg(self, i):
        return self.rchg[i + 1]


def _clean_mmatch(dis, i, s, e):
    if i - s > XDL_SIMSCAN_WINDOW:
        s = i - XDL_SIMSCAN_WINDOW
    if e - i > XDL_SIMSCAN_WINDOW:
        e = i + XDL_SIMSCAN_WINDOW
    r = 1
    rdis0 = 0
    rpdis0 = 1
    while i - r >= s:
        d = dis[i - r]
        if not d:
            rdis0 += 1
        elif d == 2:
            rpdis0 += 1
        else:
            break
        r += 1
    if rdis0 == 0:
        return False
    r = 1
    rdis1 = 0
    rpdis1 = 1
    while i + r <= e:
        d = dis[i + r]
        if not d:
            rdis1 += 1
        elif d == 2:
            rpdis1 += 1
        else:
            break
        r += 1
    if rdis1 == 0:
        return False
    rdis1 += rdis0
    rpdis1 += rpdis0
    return rpdis1 * XDL_KPDIS_RUN < (rpdis1 + rdis1)


def _split(ha1, off1, lim1, ha2, off2, lim2, kvdf, kvdb, koff, need_min, mxcost):
    """xdl_split: (i1, i2, min_lo, min_hi)."""
    dmin = off1 - lim2
    dmax = lim1 - off2
    fmid = off1 - off2
    bmid = lim1 - lim2
    odd = (fmid - bmid) & 1
    fmin = fmax = fmid
    bmin = bmax = bmid
    kvdf[fmid + koff] = off1
    kvdb[bmid + koff] = lim1
    ec = 0
    while True:
        ec += 1
        got_snake = False
        if fmin > dmin:
            fmin -= 1
            kvdf[fmin - 1 + koff] = -1
        else:
            fmin += 1
        if fmax < dmax:
            fmax += 1
            kvdf[fmax + 1 + koff] = -1
        else:
            fmax -= 1
        d = fmax
        while d >= fmin:
            if kvdf[d - 1 + koff] >= kvdf[d + 1 + koff]:
                i1 = kvdf[d - 1 + koff] + 1
            else:
                i1 = kvdf[d + 1 + koff]
            prev1 = i1
            i2 = i1 - d
            while i1 < lim1 and i2 < lim2 and ha1[i1] == ha2[i2]:
                i1 += 1
                i2 += 1
            if i1 - prev1 > XDL_SNAKE_CNT:
                got_snake = True
            kvdf[d + koff] = i1
            if odd and bmin <= d <= bmax and kvdb[d + koff] <= i1:
                return i1, i2, True, True
            d -= 2
        if bmin > dmin:
            bmin -= 1
            kvdb[bmin - 1 + koff] = XDL_LINE_MAX
        else:
            bmin += 1
        if bmax < dmax:
            bmax += 1
            kvdb[bmax + 1 + koff] = XDL_LINE_MAX
        else:
            bmax -= 1
        d = bmax
        while d >= bmin:
            if kvdb[d - 1 + koff] < kvdb[d + 1 + koff]:
                i1 = kvdb[d - 1 + koff]
            else:
                i1 = kvdb[d + 1 + koff] - 1
            prev1 = i1
            i2 = i1 - d
            while i1 > off1 and i2 > off2 and ha1[i1 - 1] == ha2[i2 - 1]:
                i1 -= 1
                i2 -= 1
            if prev1 - i1 > XDL_SNAKE_CNT:
                got_snake = True
            kvdb[d + koff] = i1
            if not odd and fmin <= d <= fmax and i1 <= kvdf[d + koff]:
                return i1, i2, True, True
            d -= 2
        if need_min:
            continue
        if got_snake and ec > XDL_HEUR_MIN_COST:
            best = 0
            spl = None
            d = fmax
            while d >= fmin:
                dd = d - fmid if d > fmid else fmid - d
                i1 = kvdf[d + koff]
                i2 = i1 - d
                v = (i1 - off1) + (i2 - off2) - dd
                if (v > XDL_K_HEUR * ec and v > best and off1 + XDL_SNAKE_CNT <= i1 < lim1
                        and off2 + XDL_SNAKE_CNT <= i2 < lim2):
                    k = 1
                    while ha1[i1 - k] == ha2[i2 - k]:
                        if k == XDL_SNAKE_CNT:
                            best = v
                            spl = (i1, i2)
                            break
                        k += 1
                d -= 2
            if best > 0:
                return spl[0], spl[1], True, False
            best = 0
            d = bmax
            while d >= bmin:
                dd = d - bmid if d > bmid else bmid - d
                i1 = kvdb[d + koff]
                i2 = i1 - d
                v = (lim1 - i1) + (lim2 - i2) - dd
                if (v > XDL_K_HEUR * ec and v > best and off1 < i1 <= lim1 - XDL_SNAKE_CNT
                        and off2 < i2 <= lim2 - XDL_SNAKE_CNT):
                    k = 0
                    while ha1[i1 + k] == ha2[i2 + k]:
                        if k == XDL_SNAKE_CNT - 1:
                            best = v
                            spl = (i1, i2)
                            break
                        k += 1
                d -= 2
            if best > 0:
                return spl[0], spl[1], False, True
        if ec >= mxcost:
            fbest = fbest1 = -1
            d = fmax
            while d >= fmin:
                i1 = min(kvdf[d + koff], lim1)
                i2 = i1 - d
                if lim2 < i2:
                    i1 = lim2 + d
                    i2 = lim2
                if fbest < i1 + i2:
                    fbest = i1 + i2
                    fbest1 = i1
                d -= 2
            bbest = bbest1 = XDL_LINE_MAX
            d = bmax
            while d >= bmin:
                i1 = max(off1, kvdb[d + koff])
                i2 = i1 - d
                if i2 < off2:
                    i1 = off2 + d
                    i2 = off2
                if i1 + i2 < bbest:
                    bbest = i1 + i2
                    bbest1 = i1
                d -= 2
            if (lim1 + lim2) - bbest < fbest - (off1 + off2):
                return fbest1, fbest - fbest1, True, False
            return bbest1, bbest - bbest1, False, True


def prepare(a_lines, b_lines):
    """(XFile, XFile) with every changed line marked (xdl_do_diff, Myers)."""
    ids = {}
    cnt1 = []
    cnt2 = []
    ha1 = []
    for line in a_lines:
        k = ids.get(line)
        if k is None:
            k = ids[line] = len(cnt1)
            cnt1.append(0)
            cnt2.append(0)
        cnt1[k] += 1
        ha1.append(k)
    ha2 = []
    for line in b_lines:
        k = ids.get(line)
        if k is None:
            k = ids[line] = len(cnt1)
            cnt1.append(0)
            cnt2.append(0)
        cnt2[k] += 1
        ha2.append(k)
    x1 = XFile(a_lines, ha1)
    x2 = XFile(b_lines, ha2)
    n1, n2 = x1.nrec, x2.nrec
    # xdl_trim_ends
    lim = min(n1, n2)
    i = 0
    while i < lim and ha1[i] == ha2[i]:
        i += 1
    dstart = i
    lim -= i
    j = 0
    while j < lim and ha1[n1 - 1 - j] == ha2[n2 - 1 - j]:
        j += 1
    dend1 = n1 - j - 1
    dend2 = n2 - j - 1
    # xdl_cleanup_records
    dis1 = [0] * (n1 + 1)
    dis2 = [0] * (n2 + 1)
    mlim = min(_bogosqrt(n1), XDL_MAX_EQLIMIT)
    for i in range(dstart, dend1 + 1):
        nm = cnt2[ha1[i]]
        dis1[i] = 0 if nm == 0 else (2 if nm >= mlim else 1)
    mlim = min(_bogosqrt(n2), XDL_MAX_EQLIMIT)
    for i in range(dstart, dend2 + 1):
        nm = cnt1[ha2[i]]
        dis2[i] = 0 if nm == 0 else (2 if nm >= mlim else 1)
    rindex1, rha1 = [], []
    for i in range(dstart, dend1 + 1):
        if dis1[i] == 1 or (dis1[i] == 2 and not _clean_mmatch(dis1, i, dstart, dend1)):
            rindex1.append(i)
            rha1.append(ha1[i])
        else:
            x1.rchg[i + 1] = 1
    rindex2, rha2 = [], []
    for i in range(dstart, dend2 + 1):
        if dis2[i] == 1 or (dis2[i] == 2 and not _clean_mmatch(dis2, i, dstart, dend2)):
            rindex2.append(i)
            rha2.append(ha2[i])
        else:
            x2.rchg[i + 1] = 1
    # xdl_do_diff
    nreff1, nreff2 = len(rindex1), len(rindex2)
    ndiags = nreff1 + nreff2 + 3
    koff = nreff2 + 1
    kvdf = [0] * (ndiags + 2)
    kvdb = [0] * (ndiags + 2)
    mxcost = max(_bogosqrt(ndiags), XDL_MAX_COST_MIN)
    stack = [(0, nreff1, 0, nreff2, False)]
    rchg1, rchg2 = x1.rchg, x2.rchg
    while stack:
        off1, lim1, off2, lim2, need_min = stack.pop()
        while off1 < lim1 and off2 < lim2 and rha1[off1] == rha2[off2]:
            off1 += 1
            off2 += 1
        while off1 < lim1 and off2 < lim2 and rha1[lim1 - 1] == rha2[lim2 - 1]:
            lim1 -= 1
            lim2 -= 1
        if off1 == lim1:
            for k in range(off2, lim2):
                rchg2[rindex2[k] + 1] = 1
        elif off2 == lim2:
            for k in range(off1, lim1):
                rchg1[rindex1[k] + 1] = 1
        else:
            s1, s2, min_lo, min_hi = _split(rha1, off1, lim1, rha2, off2, lim2, kvdf, kvdb, koff,
                                            need_min, mxcost)
            stack.append((s1, lim1, s2, lim2, min_hi))
            stack.append((off1, s1, off2, s2, min_lo))
    return x1, x2


# -- compaction ----------------------------------------------------------------------------


def _get_indent(rec):
    ret = 0
    for c in rec:
        if c not in b' \t\n\v\f\r':
            return ret
        if c == 32:
            ret += 1
        elif c == 9:
            ret += 8 - ret % 8
        if ret >= MAX_INDENT:
            return MAX_INDENT
    return -1


def _measure_split(xf, split):
    if split >= xf.nrec:
        end_of_file = 1
        indent = -1
    else:
        end_of_file = 0
        indent = _get_indent(xf.recs[split])
    pre_blank = 0
    pre_indent = -1
    i = split - 1
    while i >= 0:
        pre_indent = _get_indent(xf.recs[i])
        if pre_indent != -1:
            break
        pre_blank += 1
        if pre_blank == MAX_BLANKS:
            pre_indent = 0
            break
        i -= 1
    post_blank = 0
    post_indent = -1
    i = split + 1
    while i < xf.nrec:
        post_indent = _get_indent(xf.recs[i])
        if post_indent != -1:
            break
        post_blank += 1
        if post_blank == MAX_BLANKS:
            post_indent = 0
            break
        i += 1
    return end_of_file, indent, pre_blank, pre_indent, post_blank, post_indent


def _score_add_split(m, s):
    end_of_file, mindent, pre_blank, pre_indent, mpost_blank, post_indent = m
    if pre_indent == -1 and pre_blank == 0:
        s[1] += START_OF_FILE_PENALTY
    if end_of_file:
        s[1] += END_OF_FILE_PENALTY
    post_blank = 1 + mpost_blank if mindent == -1 else 0
    total_blank = pre_blank + post_blank
    s[1] += TOTAL_BLANK_WEIGHT * total_blank
    s[1] += POST_BLANK_WEIGHT * post_blank
    indent = mindent if mindent != -1 else post_indent
    any_blanks = total_blank != 0
    s[0] += indent
    if indent == -1 or pre_indent == -1:
        pass
    elif indent > pre_indent:
        s[1] += RELATIVE_INDENT_WITH_BLANK_PENALTY if any_blanks else RELATIVE_INDENT_PENALTY
    elif indent == pre_indent:
        pass
    else:
        if post_indent != -1 and post_indent > indent:
            s[1] += RELATIVE_OUTDENT_WITH_BLANK_PENALTY if any_blanks else RELATIVE_OUTDENT_PENALTY
        else:
            s[1] += RELATIVE_DEDENT_WITH_BLANK_PENALTY if any_blanks else RELATIVE_DEDENT_PENALTY


def _score_cmp(s1, s2):
    cmp_indents = (s1[0] > s2[0]) - (s1[0] < s2[0])
    return INDENT_WEIGHT * cmp_indents + (s1[1] - s2[1])


class _Group:
    __slots__ = ('start', 'end')


def _group_init(xf):
    g = _Group()
    g.start = g.end = 0
    while xf.rchg[g.end + 1]:
        g.end += 1
    return g


def _group_next(xf, g):
    if g.end == xf.nrec:
        return -1
    g.start = g.end + 1
    g.end = g.start
    while xf.rchg[g.end + 1]:
        g.end += 1
    return 0


def _group_previous(xf, g):
    if g.start == 0:
        return -1
    g.end = g.start - 1
    g.start = g.end
    while xf.rchg[g.start - 1 + 1]:
        g.start -= 1
    return 0


def _group_slide_down(xf, g):
    if g.end < xf.nrec and xf.ha[g.start] == xf.ha[g.end]:
        xf.rchg[g.start + 1] = 0
        g.start += 1
        xf.rchg[g.end + 1] = 1
        g.end += 1
        while xf.rchg[g.end + 1]:
            g.end += 1
        return 0
    return -1


def _group_slide_up(xf, g):
    if g.start > 0 and xf.ha[g.start - 1] == xf.ha[g.end - 1]:
        g.start -= 1
        xf.rchg[g.start + 1] = 1
        g.end -= 1
        xf.rchg[g.end + 1] = 0
        while xf.rchg[g.start - 1 + 1]:
            g.start -= 1
        return 0
    return -1


class _Bug(Exception):
    pass


def change_compact(xf, xfo, indent_heuristic=True):
    g = _group_init(xf)
    go = _group_init(xfo)
    while True:
        if g.end != g.start:
            while True:
                groupsize = g.end - g.start
                end_matching_other = -1
                while not _group_slide_up(xf, g):
                    if _group_previous(xfo, go):
                        raise _Bug('group sync broken sliding up')
                earliest_end = g.end
                if go.end > go.start:
                    end_matching_other = g.end
                while True:
                    if _group_slide_down(xf, g):
                        break
                    if _group_next(xfo, go):
                        raise _Bug('group sync broken sliding down')
                    if go.end > go.start:
                        end_matching_other = g.end
                if groupsize == g.end - g.start:
                    break
            if g.end == earliest_end:
                pass
            elif end_matching_other != -1:
                while go.end == go.start:
                    if _group_slide_up(xf, g):
                        raise _Bug('match disappeared')
                    if _group_previous(xfo, go):
                        raise _Bug('group sync broken sliding to match')
            elif indent_heuristic:
                best_shift = -1
                best_score = None
                shift = earliest_end
                if g.end - groupsize - 1 > shift:
                    shift = g.end - groupsize - 1
                if g.end - INDENT_HEURISTIC_MAX_SLIDING > shift:
                    shift = g.end - INDENT_HEURISTIC_MAX_SLIDING
                while shift <= g.end:
                    score = [0, 0]
                    _score_add_split(_measure_split(xf, shift), score)
                    _score_add_split(_measure_split(xf, shift - groupsize), score)
                    if best_shift == -1 or _score_cmp(score, best_score) <= 0:
                        best_score = score
                        best_shift = shift
                    shift += 1
                while g.end > best_shift:
                    if _group_slide_up(xf, g):
                        raise _Bug('best shift unreached')
                    if _group_previous(xfo, go):
                        raise _Bug('group sync broken sliding to blank line')
        if _group_next(xf, g):
            break
        if _group_next(xfo, go):
            raise _Bug('group sync broken moving to next group')


def build_script(x1, x2):
    """[(i1, i2, chg1, chg2)] ascending (xdl_build_script)."""
    out = []
    r1, r2 = x1.rchg, x2.rchg
    i1, i2 = x1.nrec, x2.nrec
    while i1 >= 0 or i2 >= 0:
        if r1[i1 - 1 + 1] or r2[i2 - 1 + 1]:
            l1, l2 = i1, i2
            while r1[i1 - 1 + 1]:
                i1 -= 1
            while r2[i2 - 1 + 1]:
                i2 -= 1
            out.append((i1, i2, l1 - i1, l2 - i2))
        i1 -= 1
        i2 -= 1
    out.reverse()
    return out


def diff(a_lines, b_lines, indent_heuristic=True):
    """(x1, x2, script) for two line lists, compacted as `git diff` does."""
    x1, x2 = prepare(a_lines, b_lines)
    change_compact(x1, x2, indent_heuristic)
    change_compact(x2, x1, indent_heuristic)
    return x1, x2, build_script(x1, x2)


# -- emitting ------------------------------------------------------------------------------


def _def_ff(rec):
    if rec and (chr(rec[0]).isalpha() and rec[0] < 128 or rec[0] in b'_$'):
        line = rec[:80]
        n = len(line)
        while n > 0 and line[n - 1] in b' \t\n\v\f\r':
            n -= 1
        return line[:n]
    return None


def unified(a_lines, b_lines, ctx=3, funcnames=True):
    """The hunks of a unified diff (no file header) as bytes lines, git's xdl_emit_diff."""
    x1, x2, script = diff(a_lines, b_lines)
    if not script:
        return []
    out = []
    func = b''
    funclineprev = -1
    k = 0
    n = len(script)
    while k < n:
        # xdl_get_hunk: group changes no more than 2*ctx lines apart
        first = k
        last = k
        while last + 1 < n:
            nxt = script[last + 1]
            cur = script[last]
            if nxt[0] - (cur[0] + cur[2]) > 2 * ctx:
                break
            last += 1
        xch, xche = script[first], script[last]
        s1 = max(xch[0] - ctx, 0)
        s2 = max(xch[1] - ctx, 0)
        e1 = min(xche[0] + xche[2] + ctx, x1.nrec)
        e2 = min(xche[1] + xche[3] + ctx, x2.nrec)
        if funcnames:
            ln = s1 - 1
            while ln > funclineprev and ln >= 0:
                got = _def_ff(x1.recs[ln])
                if got is not None:
                    func = got
                    break
                ln -= 1
            funclineprev = s1 - 1
        c1, c2 = e1 - s1, e2 - s2
        hdr = b'@@ -' + str(s1 + 1 if c1 else s1).encode()
        if c1 != 1:
            hdr += b',' + str(c1).encode()
        hdr += b' +' + str(s2 + 1 if c2 else s2).encode()
        if c2 != 1:
            hdr += b',' + str(c2).encode()
        hdr += b' @@'
        if func:
            hdr += b' ' + func
        out.append(hdr + b'\n')
        i1 = s1
        i2 = s2
        for ci in range(first, last + 1):
            a0, b0, ca, cb = script[ci]
            while i1 < a0:
                out.append(_line(b' ', x1.recs[i1]))
                i1 += 1
                i2 += 1
            for j in range(a0, a0 + ca):
                out.append(_line(b'-', x1.recs[j]))
            for j in range(b0, b0 + cb):
                out.append(_line(b'+', x2.recs[j]))
            i1 = a0 + ca
            i2 = b0 + cb
        while i1 < e1:
            out.append(_line(b' ', x1.recs[i1]))
            i1 += 1
        k = last + 1
    return out


def _line(mark, rec):
    if rec.endswith(b'\n'):
        return mark + rec
    return mark + rec + b'\n\\ No newline at end of file\n'


def counts(a_lines, b_lines):
    """(added, deleted) lines, as --numstat counts them."""
    _, _, script = diff(a_lines, b_lines)
    return sum(c[3] for c in script), sum(c[2] for c in script)


# -- three-way merge -----------------------------------------------------------------------


class _M:
    __slots__ = ('mode', 'i0', 'chg0', 'i1', 'chg1', 'i2', 'chg2')

    def __init__(self, mode, i0, chg0, i1, chg1, i2, chg2):
        self.mode, self.i0, self.chg0, self.i1, self.chg1, self.i2, self.chg2 = (
            mode, i0, chg0, i1, chg1, i2, chg2)


def _append(changes, mode, i0, chg0, i1, chg1, i2, chg2):
    m = changes[-1] if changes else None
    if m is not None and (i1 <= m.i1 + m.chg1 or i2 <= m.i2 + m.chg2):
        if mode != m.mode:
            m.mode = 0
        m.chg0 = i0 + chg0 - m.i0
        m.chg1 = i1 + chg1 - m.i1
        m.chg2 = i2 + chg2 - m.i2
    else:
        changes.append(_M(mode, i0, chg0, i1, chg1, i2, chg2))


def _is_eol_crlf(recs, i):
    n = len(recs)
    if i < n - 1:
        r = recs[i]
        return 1 if len(r) > 1 and r[-2:-1] == b'\r' else 0
    if not n:
        return -1
    r = recs[i]
    if r and r.endswith(b'\n'):
        return 1 if len(r) > 1 and r[-2:-1] == b'\r' else 0
    if not i:
        return -1
    r = recs[i - 1]
    return 1 if len(r) > 1 and r[-2:-1] == b'\r' else 0


def _is_cr_needed(base, ours, theirs, m):
    needs = _is_eol_crlf(ours, m.i1 - 1 if m.i1 else 0)
    if needs:
        needs = _is_eol_crlf(theirs, m.i2 - 1 if m.i2 else 0)
    if needs:
        needs = _is_eol_crlf(base, 0)
    return 0 if needs < 0 else needs


def _copy(recs, i, count, needs_cr, add_nl):
    if count < 1:
        return b''
    out = b''.join(recs[i:i + count])
    if add_nl:
        last = recs[i + count - 1]
        if not last or not last.endswith(b'\n'):
            out += (b'\r' if needs_cr else b'') + b'\n'
    return out


def merge3(base, ours, theirs, name1, name2, marker_size=7):
    """(merged bytes, conflict count) for three byte strings (xdl_merge, ZEALOUS, style merge)."""
    b_lines, o_lines, t_lines = split_lines(base), split_lines(ours), split_lines(theirs)
    _, _, s1 = diff(b_lines, o_lines)
    _, _, s2 = diff(b_lines, t_lines)
    if not s1:
        return theirs, 0
    if not s2:
        return ours, 0
    changes = []
    a, b = 0, 0
    nb, no, nt = len(b_lines), len(o_lines), len(t_lines)
    while a < len(s1) and b < len(s2):
        x1 = s1[a]
        x2 = s2[b]
        # (i1 base, i2 side, chg1 base, chg2 side)
        if x1[0] + x1[2] < x2[0]:
            _append(changes, 1, x1[0], x1[2], x1[1], x1[3], x2[1] - x2[0] + x1[0], x1[2])
            a += 1
            continue
        if x2[0] + x2[2] < x1[0]:
            _append(changes, 2, x2[0], x2[2], x1[1] - x1[0] + x2[0], x2[2], x2[1], x2[3])
            b += 1
            continue
        if (x1[0] != x2[0] or x1[2] != x2[2] or x1[3] != x2[3]
                or o_lines[x1[1]:x1[1] + x1[3]] != t_lines[x2[1]:x2[1] + x2[3]]):
            off = x1[0] - x2[0]
            ffo = off + x1[2] - x2[2]
            i0, i1, i2 = x1[0], x1[1], x2[1]
            if off > 0:
                i0 -= off
                i1 -= off
            else:
                i2 += off
            chg0 = x1[0] + x1[2] - i0
            chg1 = x1[1] + x1[3] - i1
            chg2 = x2[1] + x2[3] - i2
            if ffo < 0:
                chg0 -= ffo
                chg1 -= ffo
            else:
                chg2 += ffo
            _append(changes, 0, i0, chg0, i1, chg1, i2, chg2)
        e1 = x1[0] + x1[2]
        e2 = x2[0] + x2[2]
        if e1 >= e2:
            b += 1
        if e2 >= e1:
            a += 1
    while a < len(s1):
        x1 = s1[a]
        _append(changes, 1, x1[0], x1[2], x1[1], x1[3], x1[0] + nt - nb, x1[2])
        a += 1
    while b < len(s2):
        x2 = s2[b]
        _append(changes, 2, x2[0], x2[2], x2[0] + no - nb, x2[2], x2[1], x2[3])
        b += 1
    # xdl_refine_conflicts
    out_changes = []
    for m in changes:
        if m.mode or m.chg1 == 0 or m.chg2 == 0:
            out_changes.append(m)
            continue
        t1 = o_lines[m.i1:m.i1 + m.chg1]
        t2 = t_lines[m.i2:m.i2 + m.chg2]
        _, _, sc = diff(split_lines(b''.join(t1)), split_lines(b''.join(t2)))
        if not sc:
            m.mode = 4
            out_changes.append(m)
            continue
        i1, i2 = m.i1, m.i2
        first = True
        for (a1, a2, c1, c2) in sc:
            if first:
                m.i1, m.chg1, m.i2, m.chg2 = a1 + i1, c1, a2 + i2, c2
                out_changes.append(m)
                first = False
            else:
                out_changes.append(_M(0, m.i0, m.chg0, a1 + i1, c1, a2 + i2, c2))
    changes = out_changes
    # xdl_simplify_non_conflicts (simplify_if_no_alnum off at level ZEALOUS)
    k = 0
    while k + 1 < len(changes):
        m, nx = changes[k], changes[k + 1]
        begin = m.i1 + m.chg1
        end = nx.i1
        if m.mode != 0 or nx.mode != 0 or end - begin > 3:
            k += 1
        else:
            m.chg1 = nx.i1 + nx.chg1 - m.i1
            m.chg2 = nx.i2 + nx.chg2 - m.i2
            del changes[k + 1]
    # xdl_fill_merge_buffer
    out = []
    conflicts = 0
    i = 0
    lt = b'<' * marker_size
    eq = b'=' * marker_size
    gt = b'>' * marker_size
    for m in changes:
        if m.mode == 0:
            conflicts += 1
            needs_cr = _is_cr_needed(b_lines, o_lines, t_lines, m)
            nl = (b'\r' if needs_cr else b'') + b'\n'
            out.append(_copy(o_lines, i, m.i1 - i, 0, 0))
            out.append(lt + (b' ' + name1.encode('utf-8') if name1 is not None else b'') + nl)
            out.append(_copy(o_lines, m.i1, m.chg1, needs_cr, 1))
            out.append(eq + nl)
            out.append(_copy(t_lines, m.i2, m.chg2, needs_cr, 1))
            out.append(gt + (b' ' + name2.encode('utf-8') if name2 is not None else b'') + nl)
        elif m.mode & 3:
            out.append(_copy(o_lines, i, m.i1 - i, 0, 0))
            if m.mode & 1:
                needs_cr = _is_cr_needed(b_lines, o_lines, t_lines, m)
                out.append(_copy(o_lines, m.i1, m.chg1, needs_cr, m.mode & 2))
            if m.mode & 2:
                out.append(_copy(t_lines, m.i2, m.chg2, 0, 0))
        else:
            continue
        i = m.i1 + m.chg1
    out.append(_copy(o_lines, i, len(o_lines) - i, 0, 0))
    return b''.join(out), conflicts
