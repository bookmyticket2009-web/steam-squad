"""The four combos from the Combo Menu poster.

Prices and on/off switches live in the database (menu_items, category "Combo"), so you can switch a combo
off or track its stock like any other item. What a combo CONTAINS and which choices a customer may make is
defined here, and the server checks every order against it. A combo also uses up the plates it is made of
(momos, fries, sauces), so stock stays honest.
"""
FRY_EXTRA = 10                                                      # Rs added when momos are fried
SAUCES = ["Schezwan Sauce", "Tandoori Sauce", "Peri Peri Sauce", "Cheese"]   # dip names, as on the menu


def _item(name, variant="Regular"):
    return ("menu", name, variant)


def _dip(name):
    return ("dip", name, None)


def _sauce_option(label="Choose any one sauce"):
    return {"key": "sauce", "label": label, "choices": [{"value": s, "part": _dip(s)} for s in SAUCES]}


COMBO_SPECS = {
    "Classic Crunch": {
        "badge": "Good entry combo", "tone": "green", "image": "combo-classic-crunch",
        "lines": ["Veg Momos (Fry + ₹10)", "Salted Fries", "Any one sauce"], "note": "Fried momos included (+₹10).",
        "cooking": "Fry",
        "fixed": lambda v: [_item("Veg Momos", "Veg"), _item("Salted Fries")],
        "options": [_sauce_option()],
        "describe": lambda v, c: f"Veg Momos (Fry) + Salted Fries + {c['sauce']}",
    },
    "Fire & Crunch": {
        "badge": "Spicy bestseller potential!", "tone": "red", "image": "combo-fire-and-crunch",
        "lines": ["Peri Peri OR Tandoori Momos (Fry + ₹10)", "Peri Peri Fries OR Cheesy Fries", "Any one sauce"], "note": "Fried momos included (+₹10).",
        "cooking": "Fry",
        "fixed": lambda v: [],
        "options": [
            {"key": "momos", "label": "Choose your momos (fried)", "choices": [{"value": n, "part": _item(n, "Veg")} for n in ("Peri Peri Momos", "Tandoori Momos")]},
            {"key": "fries", "label": "Choose your fries", "choices": [{"value": n, "part": _item(n)} for n in ("Peri Peri Fries", "Cheesy Fries")]},
            _sauce_option(),
        ],
        "describe": lambda v, c: f"{c['momos']} (Fry) + {c['fries']} + {c['sauce']}",
    },
    "Cheese Overload": {
        "badge": "Premium combo", "tone": "purple", "image": "combo-cheese-overload",
        "lines": ["Cheese Loaded Momos", "Cheesy Fries", "Cheese Sauce"], "note": "Steamed or fried? Fry adds ₹10.",
        "cooking": None,
        "fixed": lambda v: [_item("Cheese Loaded Momos", "Veg"), _item("Cheesy Fries"), _dip("Cheese")],
        "options": [{"key": "cooking", "label": "Momos steamed or fried?", "choices": [
            {"value": "Steam", "label": "Steam · ₹0 extra", "extra": 0}, {"value": "Fry", "label": f"Fry · +₹{FRY_EXTRA}", "extra": FRY_EXTRA}]}],
        "describe": lambda v, c: f"Cheese Loaded Momos ({c['cooking']}) + Cheesy Fries + Cheese",
    },
    "The Ultimate Squad": {
        "badge": "Best for sharing with your squad!", "tone": "gold", "image": "combo-ultimate-squad",
        "lines": ["Peri Peri Momos + Tandoori Momos + Cheese Loaded Momos", "Peri Peri Fries", "All four sauces"], "note": "",
        "cooking": None,
        "fixed": lambda v: [_item("Peri Peri Momos", v), _item("Tandoori Momos", v), _item("Cheese Loaded Momos", v), _item("Peri Peri Fries")] + [_dip(s) for s in SAUCES],
        "options": [],
        "describe": lambda v, c: "Peri Peri + Tandoori + Cheese Loaded Momos + Peri Peri Fries + all 4 sauces",
    },
}


def validate_combo_choices(spec, raw):
    """Only choices the poster offers are accepted; anything else is an error."""
    raw = raw if isinstance(raw, dict) else {}
    if set(raw) - {o["key"] for o in spec["options"]}:
        raise ValueError("Invalid combo choices.")
    out = {}
    for opt in spec["options"]:
        if raw.get(opt["key"]) not in [c["value"] for c in opt["choices"]]:
            raise ValueError(f"Please choose: {opt['label']}.")
        out[opt["key"]] = raw[opt["key"]]
    return out


def _picked(spec, choices):
    for opt in spec["options"]:
        for c in opt["choices"]:
            if c["value"] == choices.get(opt["key"]):
                yield c


def combo_extra(spec, choices):
    return sum(c.get("extra", 0) for c in _picked(spec, choices))


def combo_parts(spec, variant, choices):
    """Every plate/portion/sauce one combo uses up: [(kind, name, variant), ...]."""
    return list(spec["fixed"](variant)) + [c["part"] for c in _picked(spec, choices) if c.get("part")]


def combos_for_page(menu, dips):
    """What the menu page needs: each combo, its choices, and whether it can be ordered right now."""
    items = {(m["name"], m["variant"]): m for m in menu}
    sauces = {d["name"]: d for d in dips}

    def ok(part):
        kind, name, variant = part
        row = sauces.get(name) if kind == "dip" else items.get((name, variant))
        return bool(row and row["orderable"])

    cards = {}
    for m in menu:
        spec = COMBO_SPECS.get(m["name"]) if m["category"] == "Combo" else None
        if not spec:
            continue
        usable = all(ok(p) for p in spec["fixed"](m["variant"]))
        options = []
        for opt in spec["options"]:
            choices = [{"value": c["value"], "label": c.get("label", c["value"]), "extra": c.get("extra", 0), "ok": ok(c["part"]) if c.get("part") else True}
                       for c in opt["choices"]]
            usable = usable and any(c["ok"] for c in choices)
            options.append({"key": opt["key"], "label": opt["label"], "choices": choices})
        card = cards.setdefault(m["name"], {"name": m["name"], "spec": spec, "tiles": []})
        card["tiles"].append({"item": m, "options": options, "orderable": bool(m["orderable"] and usable)})
    return [cards[n] for n in COMBO_SPECS if n in cards]
