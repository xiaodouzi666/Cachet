"""Fit the measured cold-prefill curve used by pricing and hosted validation."""


def fit(bands):
    # Nonnegative least squares for two features, n and n², with zero intercept.
    data = [(b["prefix_tokens"] / 1000, b["cold_p50_ms"]) for b in bands]
    a = sum(x**2 for x, y in data)
    b = sum(x**3 for x, y in data)
    c = sum(x**4 for x, y in data)
    d = sum(x*y for x, y in data)
    e = sum(x*x*y for x, y in data)
    det = a*c-b*b
    candidates = [(d/a, 0), (0, e/c), ((d*c-b*e)/det, (a*e-b*d)/det)]
    linear, quadratic = min((p for p in candidates if min(p) >= 0),
        key=lambda p: sum((p[0]*x + p[1]*x*x - y)**2 for x,y in data))
    return {"alpha": linear/1000, "beta": quadratic/1_000_000, "units": "milliseconds, tokens"}
