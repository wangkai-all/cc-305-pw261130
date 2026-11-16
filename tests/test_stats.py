"""统计方法正确性：蒙特卡洛验证误报率/覆盖率，边界形状检查。

运行：python3 -m unittest tests.test_stats -v
"""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exp_platform import statsutil as st


class TestNorm(unittest.TestCase):
    def test_ppf_cdf_roundtrip(self):
        for p in (0.001, 0.01, 0.025, 0.05, 0.5, 0.95, 0.975, 0.999):
            self.assertAlmostEqual(st.norm_cdf(st.norm_ppf(p)), p, places=7)

    def test_known_values(self):
        self.assertAlmostEqual(st.norm_ppf(0.975), 1.959964, places=5)
        self.assertAlmostEqual(st.norm_cdf(1.96), 0.9750021, places=5)


def _simulate_daily(rng, days, n_per_day, p_c, lift, gen):
    """逐日模拟两组二项计数（正态近似），逐日返回 (x_c,n_c,x_t,n_t) 累计。"""
    xc = xt = nc = nt = 0
    for _ in range(days):
        var_c = n_per_day * p_c * (1 - p_c)
        var_t = n_per_day * (p_c + lift) * (1 - p_c - lift)
        xc += gen.gauss(n_per_day * p_c, var_c ** 0.5)
        xt += gen.gauss(n_per_day * (p_c + lift), var_t ** 0.5)
        nc += n_per_day
        nt += n_per_day
        yield xc, nc, xt, nt


class TestSequentialValidity(unittest.TestCase):
    """零假设下每日偷看：mSPRT 与组序贯守住 α，裸 z 检验膨胀。"""

    DAYS, N_DAY, P0, ALPHA = 14, 1500, 0.12, 0.05
    SIMS = 3000

    @classmethod
    def setUpClass(cls):
        rng = random.Random(7)
        cls.max_lambda, cls.max_abs_z, cls.z_paths = [], [], []
        tau2 = 0.01 ** 2
        for _ in range(cls.SIMS):
            gen = random.Random(rng.random())
            ml = mz = 0.0
            zs = []
            for xc, nc, xt, nt in _simulate_daily(gen, cls.DAYS, cls.N_DAY,
                                                  cls.P0, 0.0, gen):
                tp = st.two_prop(round(xc), nc, round(xt), nt)
                ml = max(ml, st.mixture_lr(tp["theta"], tp["V"], tau2))
                mz = max(mz, abs(tp["z"]))
                zs.append(tp["z"])
            cls.max_lambda.append(ml)
            cls.max_abs_z.append(mz)
            cls.z_paths.append(zs)

    def test_naive_peeking_inflates(self):
        """裸固定样本检验每日看：误报率显著超过 α（证明该问题真实存在）。"""
        rate = sum(1 for z in self.max_abs_z if z >= 1.96) / self.SIMS
        self.assertGreater(rate, 0.10)  # 文献值约 0.2 量级

    def test_msprt_controls_type1(self):
        """mSPRT：任意时刻越界概率 ≤ α（留模拟误差余量）。"""
        rate = sum(1 for lam in self.max_lambda if lam >= 1 / self.ALPHA) / self.SIMS
        self.assertLessEqual(rate, self.ALPHA + 0.015)

    def test_gsd_controls_type1(self):
        """组序贯 OBF：总消耗 ≈ α。"""
        t_fracs = [(k + 1) / self.DAYS for k in range(self.DAYS)]
        us = st.gsd_boundaries(t_fracs, self.ALPHA, "obf")
        rate = sum(1 for zs in self.z_paths
                   if any(abs(z) >= u for z, u in zip(zs, us))) / self.SIMS
        self.assertGreaterEqual(rate, 0.02)
        self.assertLessEqual(rate, self.ALPHA + 0.02)

    def test_cs_coverage(self):
        """置信序列：真效应被全程覆盖的概率 ≥ 1-α（留余量）。"""
        rng = random.Random(11)
        tau2, theta_true = 0.01 ** 2, 0.01
        bad = 0
        sims = 1500
        for _ in range(sims):
            gen = random.Random(rng.random())
            ok = True
            for xc, nc, xt, nt in _simulate_daily(gen, self.DAYS, self.N_DAY,
                                                  self.P0, theta_true, gen):
                tp = st.two_prop(round(xc), nc, round(xt), nt)
                half = st.cs_halfwidth(tp["V"], tau2, self.ALPHA)
                if not (tp["theta"] - half <= theta_true <= tp["theta"] + half):
                    ok = False
                    break
            bad += 0 if ok else 1
        self.assertLessEqual(bad / sims, self.ALPHA + 0.015)


class TestGsdShape(unittest.TestCase):
    def test_obf_decreasing(self):
        us = st.gsd_boundaries([0.25, 0.5, 0.75, 1.0], 0.05, "obf")
        self.assertTrue(all(a > b for a, b in zip(us, us[1:])))
        # 标准表 OBF K=4 等距 ≈ (4.05, 2.86, 2.34, 2.02)，网格积分误差 ~0.02
        self.assertAlmostEqual(us[-1], 2.02, delta=0.06)

    def test_pocock_flatter(self):
        us = st.gsd_boundaries([0.25, 0.5, 0.75, 1.0], 0.05, "pocock")
        self.assertLess(max(us) - min(us), 0.5)

    def test_spending_functions(self):
        self.assertAlmostEqual(st.alpha_spent(1.0, 0.05, "obf"), 0.05, places=6)
        self.assertAlmostEqual(st.alpha_spent(1.0, 0.05, "pocock"), 0.05, places=6)
        self.assertLess(st.alpha_spent(0.5, 0.05, "obf"),
                        st.alpha_spent(0.5, 0.05, "pocock"))


if __name__ == "__main__":
    unittest.main()
