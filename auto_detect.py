"""Tries to relocate the calibrated UI elements automatically by matching template
images saved by calibration.py against the current screen. Used by the 'Testar
calibração automática' button, and by automation.py on every search (to click the
right spot even if the layout has shifted a bit).

Each element can have MULTIPLE saved appearance variants (e.g. the site's normal
wide layout vs. its narrow/responsive layout, which changes how some buttons look).
Variants are stored as templates/<name>/1.png, 2.png, ... and are all tried in
turn -- new variants only ever get ADDED (see calibration.py), so recalibrating
for a new layout never throws away one that already works for another layout.

Each element also keeps a single consolidated hit-rate file (templates/<name>/stats.json,
keyed by filename) that's updated every time one of its variants is actually tried
against the screen. variant_paths() uses it to try the best-performing variant
first, and a variant that misses too many times IN A ROW with no success in
between gets deleted automatically -- see DISCARD_AFTER_CONSECUTIVE_FAILURES and
_record_attempt.
"""
import json
import time
from pathlib import Path

import pyautogui

import citrix_utils

TEMPLATES_DIR = Path(__file__).parent / "templates"
CONFIG_PATH = Path(__file__).parent / "config.json"

# The session watermark (timestamp/IP text) drifts across the page and occasionally
# overlaps one of these elements right at screenshot time, which can drop the match
# confidence just enough to miss. A lower threshold + a couple of quick retries
# (the watermark won't be in the exact same spot a few hundred ms later) makes this
# reliable without needing a stricter/slower detection method.
CONFIDENCE = 0.80
RETRIES = 3
RETRY_DELAY_SECONDS = 0.4
MAX_DRIFT_PX = 60  # see try_auto_calibrate's _guarded_point

POINT_ELEMENTS = ["cep_field", "numero_field", "pesquisar_button"]
REGION_ELEMENTS = ["table_row_region"]

# A variant that fails this many times IN A ROW (no success in between) gets
# deleted -- see _record_attempt. Deliberately high: a variant can legitimately
# go a long stretch without matching (e.g. it's the narrow-layout appearance and
# the wide layout has been showing for a while), so this should only catch
# variants that are essentially never going to work again, not ones that are
# just temporarily out of rotation.
DISCARD_AFTER_CONSECUTIVE_FAILURES = 40

# Only these elements get effectiveness tracking/reordering/discarding.
# 'no_data_banner' is deliberately excluded: it's matched against every search
# result, and "not found" there just means the record HAS data (a normal, common
# business outcome) -- not a sign the template stopped working, so treating those
# misses as detection failures would get a perfectly good template discarded.
_TRACKED_ELEMENTS = set(POINT_ELEMENTS) | set(REGION_ELEMENTS)


def _variant_dir(name) -> Path:
    return TEMPLATES_DIR / name


_DEFAULT_STATS = {"attempts": 0, "successes": 0, "consecutive_failures": 0}


def _stats_file(name: str) -> Path:
    return _variant_dir(name) / "stats.json"


def _load_all_stats(name: str) -> dict:
    """All variants' stats for this element in one dict, keyed by filename
    (e.g. '1.png'). Transparently migrates old one-file-per-variant
    '<n>.stats.json' sidecars into this consolidated file the first time this
    element is touched (a leftover from before stats were consolidated), then
    removes them so they don't keep piling up in the folder."""
    consolidated_path = _stats_file(name)
    data = {}
    if consolidated_path.exists():
        try:
            data = json.loads(consolidated_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    d = _variant_dir(name)
    legacy_sidecars = list(d.glob("*.stats.json")) if d.exists() else []
    if legacy_sidecars:
        for sidecar in legacy_sidecars:
            png_name = sidecar.name[: -len(".stats.json")] + ".png"
            if png_name not in data:
                try:
                    data[png_name] = json.loads(sidecar.read_text(encoding="utf-8"))
                except Exception:
                    pass
            sidecar.unlink(missing_ok=True)
        _save_all_stats(name, data)
    return data


def _save_all_stats(name: str, all_stats: dict):
    _stats_file(name).write_text(json.dumps(all_stats, indent=2), encoding="utf-8")


def _success_rate(stats: dict) -> float:
    """Laplace-smoothed hit rate, so a variant with few attempts so far isn't
    unfairly ranked first (a single lucky match) or last (a single early miss)
    ahead of ones with a long, solid track record."""
    return (stats["successes"] + 1) / (stats["attempts"] + 2)


def _record_attempt(name: str, path: Path, matched: bool, log):
    """Update the variant's hit-rate stats after actually trying to match it on
    screen, and delete it if it's crossed the no-longer-useful threshold. Never
    discards the last remaining variant for an element -- a consistently failing
    template is still better than none (has_templates() would otherwise start
    failing and force a full manual recalibration)."""
    if name not in _TRACKED_ELEMENTS or not path.exists():
        return
    all_stats = _load_all_stats(name)
    stats = dict(_DEFAULT_STATS, **all_stats.get(path.name, {}))
    stats["attempts"] += 1
    if matched:
        stats["successes"] += 1
        stats["consecutive_failures"] = 0
    else:
        stats["consecutive_failures"] += 1

    if stats["consecutive_failures"] >= DISCARD_AFTER_CONSECUTIVE_FAILURES and len(variant_paths(name)) > 1:
        log(f"  Descartando variante '{name}/{path.name}': não bateu em {stats['consecutive_failures']} "
            f"tentativas seguidas -- provavelmente um layout antigo que não existe mais. "
            f"Recalibre manualmente se essa aparência ainda for válida.")
        path.unlink(missing_ok=True)
        all_stats.pop(path.name, None)
        _save_all_stats(name, all_stats)
        return

    all_stats[path.name] = stats
    _save_all_stats(name, all_stats)


def variant_paths(name):
    """All saved appearance variants for an element, e.g. templates/pesquisar_button/1.png, 2.png...
    -- ordered by hit rate (best first) for tracked elements, so the common case
    (the variant that's been working) gets tried first instead of always paying
    for a full sweep."""
    d = _variant_dir(name)
    if not d.exists():
        return []
    paths = sorted(d.glob("*.png"), key=lambda p: p.stem)
    if name in _TRACKED_ELEMENTS:
        all_stats = _load_all_stats(name)
        paths = sorted(
            paths,
            key=lambda p: _success_rate(dict(_DEFAULT_STATS, **all_stats.get(p.name, {}))),
            reverse=True,
        )
    return paths


def next_variant_path(name) -> Path:
    """Where the NEXT new variant for this element should be saved (never overwrites)."""
    d = _variant_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    existing = [int(p.stem) for p in d.glob("*.png") if p.stem.isdigit()]
    n = (max(existing) + 1) if existing else 1
    return d / f"{n}.png"


def _click_ratio(png_path: Path):
    """Where inside the matched template box the actual click point sits, as a
    (x_ratio, y_ratio) fraction of (width, height) from the top-left. Defaults to
    dead-center (0.5, 0.5), which is correct when the template was cropped
    symmetrically around the click point (calibration.py's own convention).
    Templates that include extra context (e.g. a label above the input box) can
    ship a sidecar '<n>.click.json' next to '<n>.png' with a different ratio."""
    sidecar = png_path.with_suffix(".click.json")
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            return data["x_ratio"], data["y_ratio"]
        except Exception:
            pass
    return 0.5, 0.5


def _locate_box(name, log, region=None):
    """Try every saved appearance variant of `name`, retrying a few full passes
    (transient watermark overlap can drop a match momentarily). Pass `region`
    (left, top, width, height) to constrain the search to a specific window --
    keeps a multi-window desktop (other apps, a second Citrix session) from
    matching something that isn't actually inside the app's own window.
    Returns (pyscreeze Box, variant_path) or (None, None)."""
    paths = variant_paths(name)
    if not paths:
        log(f"  Sem template salvo para '{name}'.")
        return None, None
    for attempt in range(1, RETRIES + 1):
        for path in paths:
            try:
                kwargs = {"confidence": CONFIDENCE}
                if region is not None:
                    kwargs["region"] = region
                box = pyautogui.locateOnScreen(str(path), **kwargs)
            except Exception:
                box = None
            _record_attempt(name, path, matched=(box is not None), log=log)
            if box is not None:
                return box, path
        if attempt < RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)
    log(f"  Não encontrei '{name}' na tela após tentar {len(paths)} variante(s) de aparência "
        f"({RETRIES}x cada). Pode ser um layout novo -- rode a calibração manual pra ensinar essa aparência.")
    return None, None


def _locate_point(name, log, region=None):
    box, path = _locate_box(name, log, region=region)
    if box is None:
        return None
    rx, ry = _click_ratio(path)
    x = box.left + box.width * rx
    y = box.top + box.height * ry
    return pyautogui.Point(int(x), int(y))


def _locate_region(name, log, region=None):
    box, _path = _locate_box(name, log, region=region)
    return box


def has_templates() -> bool:
    if not TEMPLATES_DIR.exists():
        return False
    return all(variant_paths(n) for n in POINT_ELEMENTS + REGION_ELEMENTS)


def try_auto_calibrate(log=print) -> bool:
    """Returns True if every element was found and config.json was (re)written."""
    if not has_templates():
        log("Ainda não há templates salvos (rode a calibração manual pelo menos uma vez primeiro).")
        return False

    log("Procurando os elementos na tela pela aparência salva (sem precisar clicar)...")

    cep_pos = _locate_point("cep_field", log)
    if cep_pos is None:
        return False
    log(f"  Campo CEP encontrado em {cep_pos}.")

    win = citrix_utils.find_window_at_point(cep_pos.x, cep_pos.y)
    if win is None:
        log("  Encontrei o campo CEP, mas não identifiquei a janela por trás dele.")
        return False
    log(f"  Janela identificada: '{win.title}' em {win.left},{win.top}.")

    numero_pos = _locate_point("numero_field", log)
    if numero_pos is None:
        return False
    log(f"  Campo Número encontrado em {numero_pos}.")

    btn_pos = _locate_point("pesquisar_button", log)
    if btn_pos is None:
        return False
    log(f"  Botão Pesquisar encontrado em {btn_pos}.")

    row_box = _locate_region("table_row_region", log)
    if row_box is None:
        return False
    log(f"  Região da tabela encontrada em {row_box}.")

    old_config = {}
    if CONFIG_PATH.exists():
        try:
            old_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _guarded_point(name, new_x, new_y):
        """Keep the freshly-detected point, UNLESS it drifted too far from the
        last manually-calibrated one for this field. cep_field and numero_field
        are near-identical blank input boxes, so appearance matching can
        confidently lock onto the WRONG one of the two (seen in practice: CEP's
        point landing on the Número field) -- and since this function is what
        (re)writes config.json on every app startup, a bad match here would
        silently overwrite a correct manual calibration before the user ever
        gets to run anything. When that happens, keep the old point and flag it
        instead of trusting the new one."""
        old = old_config.get(name)
        if old is not None:
            dx, dy = new_x - old["x"], new_y - old["y"]
            if (dx * dx + dy * dy) ** 0.5 > MAX_DRIFT_PX:
                log(f"  Aviso: detecção por imagem de '{name}' ({new_x},{new_y}) ficou longe da "
                    f"última calibração manual ({old['x']},{old['y']}) -- mantendo a calibração manual. "
                    f"Se o layout realmente mudou, recalibre manualmente.")
                return {"x": old["x"], "y": old["y"]}
        return {"x": new_x, "y": new_y}

    def _guarded_region(name, new_x1, new_y1, new_x2, new_y2):
        """Same guard as _guarded_point, for the region element. A region
        match landing on the wrong spot silently corrupts the table-reading
        crop (seen in practice: stable garbage OCR output like 'rq |' every
        single attempt, because the crop was consistently pointed at the wrong
        static content instead of the results table)."""
        old = old_config.get(name)
        if old is not None:
            dx, dy = new_x1 - old["x1"], new_y1 - old["y1"]
            if (dx * dx + dy * dy) ** 0.5 > MAX_DRIFT_PX:
                log(f"  Aviso: detecção por imagem de '{name}' ficou longe da última calibração "
                    f"manual -- mantendo a calibração manual. Se o layout realmente mudou, "
                    f"recalibre manualmente.")
                return dict(old)
        return {"x1": new_x1, "y1": new_y1, "x2": new_x2, "y2": new_y2}

    config = {
        "window_title_contains": citrix_utils.stable_title_anchor(win.title),
        "wait_after_search_seconds": old_config.get("wait_after_search_seconds", 1.0),
        "delay_between_cnpjs_seconds": old_config.get("delay_between_cnpjs_seconds", 0.7),
        "tesseract_cmd": old_config.get("tesseract_cmd", r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        "cep_field": _guarded_point("cep_field", int(cep_pos.x - win.left), int(cep_pos.y - win.top)),
        "numero_field": _guarded_point("numero_field", int(numero_pos.x - win.left), int(numero_pos.y - win.top)),
        "pesquisar_button": {"x": int(btn_pos.x - win.left), "y": int(btn_pos.y - win.top)},
        "table_row_region": _guarded_region(
            "table_row_region",
            int(row_box.left - win.left), int(row_box.top - win.top),
            int(row_box.left + row_box.width - win.left),
            int(row_box.top + row_box.height - win.top),
        ),
    }

    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Calibração automática concluída e salva em {CONFIG_PATH}.")
    return True
