"""Does combining your own driving with BYD consumption beat your own driving alone? Simulator experiment.

    py tools/fusion_experiment.py        (run from cloud/; takes about a minute)

Compares the centre error of the own-only and the combined estimate at several distances, with the battery size
right and wrong by 7%, in a dense-polling drive and in a drive whose first 12 km were not reported. Results are in
docs/PROJECT_PLAN.md section 6m."""
import sys, random, statistics
import os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..")); sys.path.insert(0, os.path.join(HERE, "..", "tests"))
import test_range_engine as t
import range_engine as r

# monkeypatch simulate to also keep the true SOC at every reading
def simulate_true(rng, km_driven, start_soc=95.0, poll_km=1.0, cv=0.10, cons=t.TRUE_CONS, odo0=1000.0):
    soc_true, odo = start_soc, odo0
    readings = [{"soc": round(soc_true), "odometer_km": float(round(odo)), "true_soc": soc_true, "true_odo": odo}]
    chunk, since, done = cons, 0.0, 0.0
    while done < km_driven:
        if since <= 0:
            chunk = cons * max(0.5, rng.gauss(1.0, cv)); since = 5.0
        step = min(poll_km, km_driven - done)
        soc_true -= chunk * step / 100.0 / t.CAP * 100.0
        odo += step; done += step; since -= step
        readings.append({"soc": round(soc_true), "odometer_km": float(round(odo)), "true_soc": soc_true, "true_odo": odo})
    return readings, soc_true

def byd_rate(readings):
    """BYD's last-50-km figure: real energy over the last <=50 km, rounded to 0.1."""
    end = readings[-1]
    j = 0
    for i, x in enumerate(readings):
        if end["true_odo"] - x["true_odo"] <= 50:
            j = i; break
    km = end["true_odo"] - readings[j]["true_odo"]
    if km < 1: return None
    kwh = (readings[j]["true_soc"] - end["true_soc"]) / 100 * t.CAP
    return round(kwh / km * 100, 1)

print("km driven | mean abs error of the CENTER (vs true mean-consumption range), % | coverage of nominal-80% band vs the real future")
print("          | own only | fused cap exact | fused cap -7% | fused cap +7% |  own cov | fused cov (exact cap)")
for km in (20, 30, 45, 70, 120):
    e_own, e0, em, ep, cov_own, cov_f, n = [], [], [], [], 0, 0, 0
    for s in range(150):
        rng = random.Random(7000 + s)
        d, soc_true = simulate_true(rng, km, start_soc=96, poll_km=0.5)
        truth = t.true_range(soc_true)
        rate = byd_rate(d)
        own = r.estimate(d, usable_kwh=t.CAP)
        if own.status != "ok" or rate is None: continue
        n += 1
        e_own.append(abs(own.true_range_km / truth - 1))
        for store, fac in ((e0, 1.0), (em, 0.93), (ep, 1.07)):
            f = r.best_estimate(d, usable_kwh=t.CAP * fac, byd_kwh_per_100=rate)
            store.append(abs(f.true_range_km / truth - 1))
        f = r.best_estimate(d, usable_kwh=t.CAP, byd_kwh_per_100=rate)
        actual = t.actual_remaining_km(random.Random(9000 + s), soc_true)
        cov_own += own.low_km <= actual <= own.high_km
        cov_f += f.low_km <= actual <= f.high_km
    m = lambda v: statistics.mean(v) * 100
    print(f"{km:8d}  | {m(e_own):7.1f}  | {m(e0):14.1f}  | {m(em):12.1f}  | {m(ep):12.1f}  | {cov_own/n*100:6.0f}%  | {cov_f/n*100:6.0f}%   (n={n})")

print()
print("SPARSE START (like your real trip): car silent for the first 12 km, then reports every 0.5 km")
print("observed km | own only | fused (cap exact) | fused (cap -7%) | fused (cap +7%) | own cov | fused cov | avg weight on own")
def simulate_gap(rng, silent_km, observed_km, start_soc=96.0, poll_km=0.5, cv=0.10, cons=t.TRUE_CONS, odo0=1000.0):
    soc_true, odo = start_soc, odo0
    readings = [{"soc": round(soc_true), "odometer_km": float(round(odo)), "true_soc": soc_true, "true_odo": odo}]
    chunk, since, done = cons, 0.0, 0.0
    total = silent_km + observed_km
    while done < total:
        if since <= 0:
            chunk = cons * max(0.5, rng.gauss(1.0, cv)); since = 5.0
        step = min(poll_km, total - done)
        soc_true -= chunk * step / 100.0 / t.CAP * 100.0
        odo += step; done += step; since -= step
        if done > silent_km:
            readings.append({"soc": round(soc_true), "odometer_km": float(round(odo)), "true_soc": soc_true, "true_odo": odo})
    return readings, soc_true

for obs in (2, 5, 10, 20, 40):
    e_own, e0, em, ep, co, cf, wt, n = [], [], [], [], 0, 0, [], 0
    for s in range(200):
        d, soc_true = simulate_gap(random.Random(7000 + s), 12, obs)
        truth = t.true_range(soc_true)
        rate = byd_rate(d)
        own = r.estimate(d, usable_kwh=t.CAP)
        if own.status != "ok" or rate is None: continue
        n += 1
        e_own.append(abs(own.true_range_km / truth - 1))
        for store, fac in ((e0, 1.0), (em, 0.93), (ep, 1.07)):
            f = r.best_estimate(d, usable_kwh=t.CAP * fac, byd_kwh_per_100=rate)
            store.append(abs(f.true_range_km / truth - 1))
        f = r.best_estimate(d, usable_kwh=t.CAP, byd_kwh_per_100=rate)
        wt.append(f.parts["own_weight"])
        actual = t.actual_remaining_km(random.Random(9000 + s), soc_true)
        co += own.low_km <= actual <= own.high_km
        cf += f.low_km <= actual <= f.high_km
    if n == 0:
        print(f"{obs:10d}  | own estimate unavailable in all runs (insufficient) -> only BYD-based number possible"); continue
    m = lambda v: statistics.mean(v) * 100
    print(f"{obs:10d}  | {m(e_own):7.1f}  | {m(e0):16.1f}  | {m(em):14.1f}  | {m(ep):14.1f}  | {co/n*100:5.0f}%  | {cf/n*100:6.0f}%   | {statistics.mean(wt)*100:4.0f}%  (n={n})")
