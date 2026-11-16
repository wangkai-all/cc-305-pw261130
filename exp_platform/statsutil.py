"""序贯统计核心：mSPRT（always-valid）、置信序列、组序贯（Lan-DeMets α 消耗）边界。

纯标准库。所有函数无副作用、可单测。
方法学说明见 docs/methods.md。
"""
from __future__ import annotations

import math

SQRT2 = math.sqrt(2.0)
SQRT2PI = math.sqrt(2.0 * math.pi)


# ---------------------------------------------------------------- 正态分布

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def norm_ppf(p: float) -> float:
    """Acklam 有理逼近，|误差| ~ 1e-9 量级。"""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                 ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)


# ---------------------------------------------------------------- 两比例比较

def two_prop(x_c: int, n_c: int, x_t: int, n_t: int) -> dict | None:
    """对照/处理的转化人数与暴露人数 -> 差值 theta、标准误、精度 V、z。"""
    if n_c <= 0 or n_t <= 0:
        return None
    p_c, p_t = x_c / n_c, x_t / n_t
    theta = p_t - p_c
    se2 = p_c * (1.0 - p_c) / n_c + p_t * (1.0 - p_t) / n_t
    if se2 <= 0.0:
        se2 = 1e-12
    se = math.sqrt(se2)
    return {"p_c": p_c, "p_t": p_t, "theta": theta, "se": se,
            "V": 1.0 / se2, "z": theta / se}


# ---------------------------------------------------------------- mSPRT（正态混合）

def mixture_lr(theta_hat: float, V: float, tau2: float) -> float:
    """混合似然比 Λ：H0: θ=0 vs H1: θ~N(0, tau2)，数据 θ̂~N(θ, 1/V)。

    Λ = (1+V·τ²)^(-1/2) · exp( θ̂²·V²·τ² / (2(1+V·τ²)) )
    {1/Λ_t} 是 always-valid p 值过程的基础（任意停时 T：P(1/Λ_T <= α) <= α）。
    """
    x = 1.0 + V * tau2
    exponent = theta_hat * theta_hat * V * V * tau2 / (2.0 * x)
    return x ** -0.5 * math.exp(min(exponent, 700.0))


def always_valid_p(lambdas: list[float]) -> float:
    """p_t = min(1, min_{s<=t} 1/Λ_s)：到 t 为止的 always-valid p 值。"""
    if not lambdas:
        return 1.0
    return min(1.0, min(1.0 / lam for lam in lambdas))


def cs_halfwidth(V: float, tau2: float, alpha: float) -> float:
    """置信序列半宽：P(∀t: θ ∈ θ̂_t ± w_t) >= 1-α。

    由 Λ(θ0) < 1/α 反解：w² = (1+Vτ²)/(V²τ²) · ( ln(1+Vτ²) + 2 ln(1/α) )
    """
    x = 1.0 + V * tau2
    return math.sqrt(x / (V * V * tau2) * (math.log(x) + 2.0 * math.log(1.0 / alpha)))


# ---------------------------------------------------------------- α 消耗函数（双侧）

def alpha_spent(t: float, alpha: float, spending: str = "obf") -> float:
    """信息比例 t∈(0,1] 处累计消耗的 α。obf = O'Brien-Fleming 型，pocock = Pocock 型。"""
    if t <= 0.0:
        return 0.0
    t = min(t, 1.0)
    if spending == "pocock":
        return alpha * math.log(1.0 + (math.e - 1.0) * t)
    # obf: Lan-DeMets 逼近连续 OBF 边界 u(t)=z_{α/2}/√t
    z = norm_ppf(1.0 - alpha / 2.0)
    return 2.0 * (1.0 - norm_cdf(z / math.sqrt(t)))


# ---------------------------------------------------------------- 组序贯边界（独立增量递推）

def _convolve_gauss(dens: list[float], h: float, dt: float) -> list[float]:
    """密度与 N(0, dt) 核做离散卷积（网格间距 h）。dens 未归一化。"""
    n = len(dens)
    sigma = math.sqrt(dt)
    kw = max(1, int(4.0 * sigma / h) + 1)
    kern = [math.exp(-0.5 * ((j * h) / sigma) ** 2) / (sigma * SQRT2PI) * h
            for j in range(-kw, kw + 1)]
    out = [0.0] * n
    for i in range(n):
        acc = 0.0
        lo = max(0, i - kw)
        hi = min(n - 1, i + kw)
        for j in range(lo, hi + 1):
            acc += dens[j] * kern[i - j + kw]
        out[i] = acc
    return out


def gsd_boundaries(t_fracs: list[float], alpha: float, spending: str = "obf",
                   R: float = 6.0, h: float = 0.02) -> list[float]:
    """给定观察到的信息比例序列 t_1<...<t_K（(0,1]），递推求解各次检查的 z 边界 u_k。

    布朗运动 B(t) 尺度：B_k = Z_k·√t_k，增量独立 N(0, Δt_k)。
    第 k 次检查的越界概率 = α*(t_k) - α*(t_{k-1})（Lan-DeMets 消耗）。
    首次检查用闭式解（避免远尾离散化误差），后续用网格递推（矩形积分，
    边界与标准表误差 ~0.02 量级，对本平台的参考用途足够）。
    返回与 t_fracs 等长的 u_k 列表。
    """
    n_grid = int(2.0 * R / h) + 1
    xs = [-R + i * h for i in range(n_grid)]
    us: list[float] = []
    dens: list[float] | None = None
    prev_t = 0.0
    spent_prev = 0.0
    for t in t_fracs:
        t = min(max(t, 1e-9), 1.0)
        dt = t - prev_t
        target = alpha_spent(t, alpha, spending) - spent_prev
        if dt <= 1e-12:
            # 信息未增长：不产生新检查，沿用上一边界
            us.append(us[-1] if us else norm_ppf(1.0 - alpha / 2.0))
            continue
        if dens is None:
            # 首次检查：B_1 ~ N(0, t)，闭式解
            u1 = norm_ppf(1.0 - alpha_spent(t, alpha, spending) / 2.0)
            us.append(u1)
            b1 = u1 * math.sqrt(t)
            norm = 1.0 / math.sqrt(2.0 * math.pi * t)
            dens = [norm * math.exp(-0.5 * x * x / t) if -b1 <= x <= b1 else 0.0
                    for x in xs]
        else:
            dens = _convolve_gauss(dens, h, dt)

            def tail_mass(b: float) -> float:
                m = 0.0
                for x, f in zip(xs, dens):
                    if x > b or x < -b:
                        m += f
                return m * h

            if target <= 0.0:
                b = us[-1] * math.sqrt(prev_t)
            elif tail_mass(1e-9) <= target:
                b = 1e-9  # 剩余质量不足以支付 target：边界贴零（必然越界）
            else:
                lo, hi = 1e-9, R
                for _ in range(40):
                    mid = 0.5 * (lo + hi)
                    if tail_mass(mid) > target:
                        lo = mid
                    else:
                        hi = mid
                b = 0.5 * (lo + hi)
            us.append(b / math.sqrt(t))
            # 截断：仅保留未越界的路径进入下一次检查
            dens = [f if -b <= x <= b else 0.0 for x, f in zip(xs, dens)]
        prev_t = t
        spent_prev += max(target, 0.0)
    return us
