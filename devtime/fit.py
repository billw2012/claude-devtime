"""Least-squares fits of the plan's reported weekly share against API-equivalent cost (pure functions, stdlib only).

reported share = intercept(week) + sum over regressors of coefficient * cost

The coefficient of a regressor is the share of the week one dollar of that cost uses, so its allowance (API-equivalent
dollars per 100 % of the week) is 1 / coefficient. Every weekly window ("group") has its own intercept: the share at the
first sample of the group is not zero (the window started earlier), so each group is centred before fitting.
"""

import math

FAMILIES = ("opus", "sonnet", "haiku", "other")
MIN_FAMILY_SHARE = 0.10  # a family needs this share of the window's cost to be fitted on its own
MIN_SAMPLES = 10  # readings the per-family fit needs (all groups together)
MIN_DOF = 3  # residual degrees of freedom it needs


def family(model):
    """opus / sonnet / haiku / other, from a model name."""
    name = str(model or "").lower()
    for f in FAMILIES[:3]:
        if f in name:
            return f
    return "other"


def _invert(m):
    """Inverse of a small square matrix (Gauss-Jordan, partial pivoting), or None when it is singular."""
    n = len(m)
    a = [list(map(float, row)) + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    scale = max(abs(m[i][i]) for i in range(n)) or 1.0
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-9 * scale:
            return None
        a[c], a[p] = a[p], a[c]
        d = a[c][c]
        a[c] = [v / d for v in a[c]]
        for r in range(n):
            if r != c and a[r][c]:
                f = a[r][c]
                a[r] = [v - f * w for v, w in zip(a[r], a[c])]
    return [row[n:] for row in a]


def regress(groups, nx):
    """Fits y = intercept(group) + sum coef[j] * x[j]. `groups` is a list of groups, each a list of (y, (x1..xn)).
    Returns {coef, se, sigma, n, dof} (sigma: the residual standard error, in y units) or None when there are too few
    points or the regressors are collinear."""
    rows, ngroups = [], 0
    for g in groups:
        if not g:
            continue
        ngroups += 1
        my = sum(p[0] for p in g) / len(g)
        mx = [sum(p[1][j] for p in g) / len(g) for j in range(nx)]
        rows += [(p[0] - my, [p[1][j] - mx[j] for j in range(nx)]) for p in g]
    dof = len(rows) - ngroups - nx
    if dof < 1:
        return None
    gram = [[sum(r[1][i] * r[1][j] for r in rows) for j in range(nx)] for i in range(nx)]
    inv = _invert(gram)
    if inv is None:
        return None
    rhs = [sum(r[1][i] * r[0] for r in rows) for i in range(nx)]
    coef = [sum(inv[i][j] * rhs[j] for j in range(nx)) for i in range(nx)]
    sse = sum((r[0] - sum(c * x for c, x in zip(coef, r[1]))) ** 2 for r in rows)
    sigma2 = sse / dof
    se = [math.sqrt(max(0.0, sigma2 * inv[j][j])) for j in range(nx)]
    return {"coef": coef, "se": se, "sigma": math.sqrt(sigma2), "n": len(rows), "dof": dof}


def allowance(coef, se):
    """(allowance, +/- allowance) from a share-per-dollar coefficient and its standard error."""
    return 1 / coef, se / (coef * coef)


def by_family(groups, names=FAMILIES):
    """Per-family allowances from groups of (share, {family: cumulative cost}) points, or None when the data does not
    separate them: fewer than two families with MIN_FAMILY_SHARE of the window's cost (smaller families' cost is
    counted with the largest one), too few samples, collinear costs or a non-positive coefficient.
    Returns {"families": {family: {"allowance", "error", "share"}}, "samples", "sigma"}."""
    window = dict.fromkeys(names, 0.0)
    for g in groups:
        if len(g) >= 2:
            for f in names:
                window[f] += g[-1][1][f] - g[0][1][f]
    total = sum(window.values())
    if total <= 0:
        return None
    big = [f for f in names if window[f] >= MIN_FAMILY_SHARE * total]
    if len(big) < 2:
        return None
    top = max(big, key=lambda f: window[f])
    pts = []
    for g in groups:
        pts.append([(y, tuple(x[f] + (sum(x[o] for o in names if o not in big) if f == top else 0.0) for f in big))
                    for y, x in g])
    fit = regress(pts, len(big))
    if fit is None or fit["n"] < MIN_SAMPLES or fit["dof"] < MIN_DOF or any(c <= 0 for c in fit["coef"]):
        return None
    fams = {}
    for f, c, s in zip(big, fit["coef"], fit["se"]):
        a, e = allowance(c, s)
        fams[f] = {"allowance": a, "error": e,
                  "share": (window[f] + (total - sum(window[o] for o in big) if f == top else 0.0)) / total}
    return {"families": fams, "samples": fit["n"], "sigma": fit["sigma"]}
