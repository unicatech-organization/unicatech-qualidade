"""Core automation loop: for each record, search CEP + Número in the remote app and
classify the result as KEEP (no data found -> eligible) or ELIMINATE (data found).
CNPJ is carried along purely as the output's identifying/reference column -- the
site itself is searched by CEP + Número, not by Documento/CNPJ."""
import difflib
import json
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import pyautogui
import pytesseract

import auto_detect
import citrix_utils
import ocr_utils

try:
    import win32clipboard
    _HAS_CLIPBOARD = True
except ImportError:
    _HAS_CLIPBOARD = False


def _set_clipboard_text(text: str):
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_TEXT)
    finally:
        win32clipboard.CloseClipboard()

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

CHECKPOINT_PATH = STATE_DIR / "checkpoint.json"

STATUS_KEEP = "mantido"
STATUS_ELIMINATE = "eliminado"
STATUS_ERROR = "erro"
STATUS_INVALID_INPUT = "cep_ou_numero_invalido"

MOUSE_MOVE_SECONDS = 0.25  # visible glide instead of an instant teleport, but still quick

MAX_LOGIN_RETRIES = 2  # how many consecutive "not logged in" checks before auto-pausing
MAX_TEMPLATE_DRIFT_PX = 60  # see _locate_click_point's sanity check against the manual calibration point
OCR_RETRIES = 4


class AutomationRunner:
    def __init__(self, config: dict, records, log=print, on_progress=None, on_logout=None,
                 input_signature="default", stop_at_time=None):
        self.config = config
        pytesseract.pytesseract.tesseract_cmd = config.get(
            "tesseract_cmd", r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        )
        # records: list of {"cnpj": str, "cep": str, "numero": str}
        self.records = [r for r in records if r.get("cep") or r.get("numero")]
        self.cnpj_list = [r["cnpj"] for r in self.records]
        self._records_by_cnpj = {r["cnpj"]: r for r in self.records}
        self.log = log
        self.on_progress = on_progress or (lambda *a, **k: None)
        self.on_logout = on_logout or (lambda: None)
        self.input_signature = input_signature
        self.stop_at_time = stop_at_time  # datetime.time or None -- auto-stop once reached, checkpoint stays saved
        self._scheduled_stop_triggered = False

        self.results = {}  # cnpj -> status
        self.index = 0

        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused
        self._stop_event = threading.Event()
        self._consecutive_login_failures = 0
        self._last_window_pos = None
        self._click_cache = {}  # (win.left, win.top, element_name) -> (x, y), see _locate_click_point
        self._region_cache = {}  # (win.left, win.top, element_name) -> (x1,y1,x2,y2), see _locate_region_box
        self._scroll_offset_cache = {}  # (win.left, win.top) -> (dx, dy), see _get_scroll_offset
        self._force_recheck_login = True  # re-check on the very first item / right after a resume
        self._data_signatures = []  # cleaned "data" OCR texts seen so far, see _read_result
        self._pre_search_snapshot = {}  # (win.left, win.top) -> pre-click table snapshot, see _start_search

        self._load_checkpoint_if_matching()

    # ---------- checkpoint ----------
    def _load_checkpoint_if_matching(self):
        if not CHECKPOINT_PATH.exists():
            return
        try:
            data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        if data.get("input_signature") != self.input_signature:
            return
        self.results = data.get("results", {})
        self.index = data.get("index", 0)
        self.log(f"Checkpoint encontrado: retomando do item {self.index + 1}/{len(self.records)}.")

    def _save_checkpoint(self):
        data = {
            "input_signature": self.input_signature,
            "index": self.index,
            "results": self.results,
        }
        CHECKPOINT_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def clear_checkpoint(self):
        if CHECKPOINT_PATH.exists():
            CHECKPOINT_PATH.unlink()

    # ---------- controls ----------
    def pause(self):
        self._pause_event.clear()
        self.log("Pausado. Clique em Retomar quando estiver pronto (relogue no Citrix se precisar).")

    def resume(self):
        self._consecutive_login_failures = 0
        self._force_recheck_login = True
        self._pause_event.set()
        self.log("Retomando...")

    def stop(self):
        self._stop_event.set()
        self._pause_event.set()  # unblock if paused, so it can exit the loop

    def _check_scheduled_stop(self):
        """If a stop-at time was configured and it's been reached, stop the run
        (checkpoint keeps whatever progress was already saved, so it resumes
        from there next time)."""
        if self.stop_at_time is None or self._stop_event.is_set():
            return
        if datetime.now().time() >= self.stop_at_time:
            self._scheduled_stop_triggered = True
            self.log(f"Horário agendado ({self.stop_at_time.strftime('%H:%M')}) atingido -- "
                     "parando automaticamente e salvando o progresso.")
            self.stop()

    # ---------- window/session helpers ----------
    def _get_window(self):
        # Primary: relocate the Pesquisar button by its saved appearance and find
        # the window underneath that exact point. This is far more reliable than
        # matching by window title -- title-substring lookups can pick the wrong
        # window when more than one shares similar text (e.g. a stale/minimized
        # duplicate). Reuses the same button template that's already required for
        # clicking (and for the login check), instead of keeping a separate
        # element calibrated just for this.
        if auto_detect.has_templates():
            btn_box, _variant = auto_detect._locate_box("pesquisar_button", lambda *a, **k: None)
            if btn_box is not None:
                anchor_x = btn_box.left + btn_box.width // 2
                anchor_y = btn_box.top + btn_box.height // 2
                win = citrix_utils.find_window_at_point(anchor_x, anchor_y)
                if win is not None:
                    self._last_window_pos = (win.left, win.top)
                    return win

        win = citrix_utils.find_window(self.config["window_title_contains"])
        if win is None:
            time.sleep(0.5)  # tolerate a transient enumeration glitch before giving up
            win = citrix_utils.find_window(self.config["window_title_contains"])
        if win is None and self._last_window_pos is not None:
            # Fallback: title lookup can occasionally miss (timing/pygetwindow quirk).
            # Try the last known window position + the cep_field offset, which
            # should still land inside the window if it hasn't actually moved/closed.
            lx, ly = self._last_window_pos
            fx = lx + self.config["cep_field"]["x"]
            fy = ly + self.config["cep_field"]["y"]
            win = citrix_utils.find_window_at_point(fx, fy)
        if win is not None:
            self._last_window_pos = (win.left, win.top)
        return win

    def _is_logged_in(self, win) -> bool:
        """Checks whether the Pesquisar button is actually visible right now. If
        the session logged out (or the page hasn't finished (re)loading), the
        button genuinely isn't on screen, so appearance matching for it comes back
        empty -- a much more direct signal than OCR-reading a label, and it
        doesn't depend on Tesseract being installed at all. Falls back to
        assuming logged-in when no template was ever calibrated for the button,
        so a missing template doesn't block every run on a check it can't do."""
        if not auto_detect.variant_paths("pesquisar_button"):
            return True
        try:
            region = (win.left, win.top, win.width, win.height)
            box, _variant = auto_detect._locate_box("pesquisar_button", lambda *a, **k: None, region=region)
            return box is not None
        except Exception as exc:
            self.log(f"Aviso: falha ao checar sessão ({exc}).")
            return False

    def _get_scroll_offset(self, win):
        """How far the page has scrolled/reflowed since calibration, estimated
        from where the Pesquisar button is actually found right now vs. where it
        was calibrated. Applied as a correction to every OTHER element's
        calibrated position before trusting/comparing against it.

        Scrolling moves everything on the page by the same amount, so one
        reliable anchor's drift tells us the drift for all of them -- and
        Pesquisar is a much safer anchor for this than cep_field/numero_field,
        which look like near-identical blank boxes and can't be trusted to
        report a large drift accurately (that ambiguity is exactly what
        MAX_TEMPLATE_DRIFT_PX below guards against). Without this, a scrolled
        page would make every OTHER element's own appearance match get rejected
        by that same guard for being "too far" from its pre-scroll calibrated
        spot, silently falling back to the now-wrong pre-scroll position instead
        of the correct new one."""
        key = (win.left, win.top)
        cached = self._scroll_offset_cache.get(key)
        if cached is not None:
            return cached

        offset = (0, 0)
        if auto_detect.has_templates() and "pesquisar_button" in self.config:
            region = (win.left, win.top, win.width, win.height)
            pos = auto_detect._locate_point("pesquisar_button", lambda *a, **k: None, region=region)
            if pos is not None:
                calibrated = citrix_utils.absolute_point(
                    win, self.config["pesquisar_button"]["x"], self.config["pesquisar_button"]["y"]
                )
                offset = (pos.x - calibrated[0], pos.y - calibrated[1])

        self._scroll_offset_cache[key] = offset
        return offset

    def _locate_click_point(self, win, name: str):
        """Where to click `name` (cep_field/numero_field/pesquisar_button) for this window.

        Detecting by appearance on every single search (region-restricted to this
        window) is what makes the system adapt automatically to a layout change --
        but doing it every time is a real OCR/appearance-matching cost, so it's
        detected once per window and cached; only re-detected if the cache is
        explicitly invalidated (see _invalidate_click_cache, called when a search
        comes back as an error -- a good sign the layout may have shifted).
        """
        key = (win.left, win.top, name)
        cached = self._click_cache.get(key)
        if cached is not None:
            return cached

        config_point = None
        if name in self.config:
            config_point = citrix_utils.absolute_point(win, self.config[name]["x"], self.config[name]["y"])
            if name != "pesquisar_button":
                dx, dy = self._get_scroll_offset(win)
                config_point = (config_point[0] + dx, config_point[1] + dy)

        point = None
        if auto_detect.has_templates():
            region = (win.left, win.top, win.width, win.height)
            pos = auto_detect._locate_point(name, lambda *a, **k: None, region=region)
            if pos is not None:
                point = (pos.x, pos.y)
                # Sanity check against the (scroll-corrected) calibrated point:
                # cep_field and numero_field are near-identical blank input
                # boxes, so appearance matching can confidently lock onto the
                # WRONG one of the two (seen in practice: CEP typed into the
                # Número field). Comparing against the scroll-corrected point
                # (not the raw pre-scroll one) means this guard still does its
                # job on a scrolled page instead of rejecting every correct
                # match just for having moved with the scroll.
                if config_point is not None:
                    dx = point[0] - config_point[0]
                    dy = point[1] - config_point[1]
                    if (dx * dx + dy * dy) ** 0.5 > MAX_TEMPLATE_DRIFT_PX:
                        self.log(f"   Aviso: detecção por imagem de '{name}' achou {point}, longe "
                                 f"do ponto calibrado (já ajustado por scroll) {config_point} -- "
                                 f"usando o calibrado.")
                        point = None
        if point is None:
            point = config_point

        self._click_cache[key] = point
        return point

    def _locate_region_box(self, win, name: str, pad: int = 8):
        """Where `name` (table_row_region) is for this window, as (x1, y1, x2, y2).
        Same appearance-detection-with-cache strategy as _locate_click_point --
        fixes the table-read crop pointing at the wrong spot when the page has
        scrolled/reflowed since config.json's offsets were saved, even though
        window-finding itself (which already re-detects fresh every time) still
        works fine."""
        key = (win.left, win.top, name)
        cached = self._region_cache.get(key)
        if cached is not None:
            return cached

        config_box = None
        if name in self.config:
            config_box = citrix_utils.absolute_region(win, self.config[name], pad=pad)
            dx0, dy0 = self._get_scroll_offset(win)
            config_box = (config_box[0] + dx0, config_box[1] + dy0, config_box[2] + dx0, config_box[3] + dy0)

        box = None
        if auto_detect.has_templates():
            region = (win.left, win.top, win.width, win.height)
            b = auto_detect._locate_region(name, lambda *a, **k: None, region=region)
            if b is not None:
                box = (b.left - pad, b.top - pad, b.left + b.width + pad, b.top + b.height + pad)
                # Same sanity check as _locate_click_point: don't trust an
                # appearance match that landed far from the (scroll-corrected)
                # calibrated region -- seen in practice as table-read OCR
                # returning the same garbage every attempt because the crop was
                # pinned to the wrong static spot on screen instead of the
                # results table.
                if config_box is not None:
                    dx = box[0] - config_box[0]
                    dy = box[1] - config_box[1]
                    if (dx * dx + dy * dy) ** 0.5 > MAX_TEMPLATE_DRIFT_PX:
                        self.log(f"   Aviso: detecção por imagem de '{name}' achou {box}, longe "
                                 f"da região calibrada (já ajustada por scroll) {config_box} -- "
                                 f"usando a calibrada.")
                        box = None
        if box is None:
            box = config_box

        self._region_cache[key] = box
        return box

    def _invalidate_click_cache(self, win):
        key_prefix = (win.left, win.top)
        for key in [k for k in self._click_cache if k[:2] == key_prefix]:
            del self._click_cache[key]
        for key in [k for k in self._region_cache if k[:2] == key_prefix]:
            del self._region_cache[key]
        self._scroll_offset_cache.pop(key_prefix, None)

    # ---------- per-record processing ----------
    def _fill_field(self, win, field_name: str, value: str):
        """Fill the field via a single clipboard paste (one paste action for the
        whole value) when available -- across every debug screenshot gathered
        while chasing field-corruption reports, paste never once actually failed
        to land the correct value. Typing (simulating individual keystrokes) is
        only used as a fallback when clipboard isn't available at all, since it
        has an actual proven failure mode: on some PCs/sessions, typed keystrokes
        get registered twice by the remote app regardless of speed (e.g. '776'
        arrives as '777766').

        There used to be an OCR readback here to verify the fill and retry on
        mismatch, but it was doing more harm than good: it never once caught a
        genuine paste failure, it regularly misread short values (e.g. read
        '196' for a correct, clearly-legible '26'), and "retrying" on those
        false alarms -- by re-clicking/re-pasting -- was the actual source of
        every real corruption seen (the typing-duplication bug above, and
        re-clicks landing wrong mid-fill)."""
        x, y = self._locate_click_point(win, field_name)
        pyautogui.click(x, y, duration=MOUSE_MOVE_SECONDS)
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("delete")
        time.sleep(0.1)
        if not value:
            return

        if _HAS_CLIPBOARD:
            _set_clipboard_text(value)
            time.sleep(0.05)
            pyautogui.hotkey("ctrl", "v")
        else:
            pyautogui.typewrite(value, interval=0.05)
        time.sleep(0.15)

    def _start_search(self, cnpj: str, win):
        """Fill CEP + Número and hit Pesquisar. Does NOT wait for the result --
        the caller is responsible for waiting before reading it back."""
        record = self._records_by_cnpj[cnpj]

        self._fill_field(win, "cep_field", record["cep"])
        self._fill_field(win, "numero_field", record["numero"])

        # Snapshot the table region right before clicking Pesquisar, so
        # _read_result can tell whether it's still looking at the PREVIOUS
        # search's table (page hasn't refreshed yet) instead of trusting
        # whatever's on screen the instant the fixed post-click wait elapses.
        rx1, ry1, rx2, ry2 = self._locate_region_box(win, "table_row_region")
        self._pre_search_snapshot[(win.left, win.top)] = ocr_utils.grab_region_gray(rx1, ry1, rx2, ry2)

        # Detected right before clicking (not up front) -- some layouts style the
        # button differently while the fields are still empty vs. filled in.
        btn_x, btn_y = self._locate_click_point(win, "pesquisar_button")
        pyautogui.click(btn_x, btn_y, duration=MOUSE_MOVE_SECONDS)

    @staticmethod
    def _clean_for_signature(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    def _looks_like_watermark_noise(self, text: str) -> bool:
        """A 'data' classification should mean genuine, per-record table content --
        but a session watermark (username/IP overlaid on the page, drifting across
        it -- see auto_detect.py's CONFIDENCE comment) sitting inside the table
        crop can read as legible OCR text that happens not to match the 'no data'
        phrase, wrongly defaulting to 'data' (seen in practice: eliminating a
        client based on watermark garbage). Real data legitimately varies company
        to company; the watermark doesn't. So: if this reading is nearly
        identical to a 'data' reading already seen for a DIFFERENT record in this
        same run, it's almost certainly static noise, not this record's data."""
        cleaned = self._clean_for_signature(text)
        if len(cleaned) < 6:
            return False
        for seen in self._data_signatures:
            if difflib.SequenceMatcher(None, cleaned, seen).ratio() >= 0.88:
                return True
        return self._has_internal_repeat(cleaned)

    @staticmethod
    def _has_internal_repeat(cleaned: str, chunk_len: int = 7) -> bool:
        """Catches watermark noise on its very FIRST appearance (before there's
        any prior reading to compare against): a watermark tiles/repeats within
        the page, so it tends to show the same chunk of garbled text twice
        within the SAME crop (e.g. literally 'ODA Seal) (1) ODA Seal) (1)').
        Genuine table content (column headers, an address, a name) doesn't
        repeat a 7+ character run of itself like that."""
        n = len(cleaned)
        if n < chunk_len * 2:
            return False
        seen_chunks = set()
        for i in range(n - chunk_len + 1):
            chunk = cleaned[i:i + chunk_len]
            if chunk in seen_chunks:
                return True
            seen_chunks.add(chunk)
        return False

    def _classify_table_by_image(self, win):
        """Recognize whether the table shows 'no data' by the on-screen
        APPEARANCE of that banner (see calibration.run_calibration, part 2/3)
        instead of OCR-reading the row's text. Much more tolerant of the session
        watermark than character-level OCR -- it only needs to match the overall
        shape (same appearance-matching-with-retries as auto_detect already uses
        for buttons/fields, which already proved workable against this same
        watermark), not make out individual letters.

        Only distinguishes empty vs. has-data -- whether the CEP/Número itself
        was invalid doesn't change the KEEP/ELIMINATE decision, so that state
        isn't checked here (it still gets detected by the OCR fallback below
        when these banner templates aren't calibrated at all).

        Returns "empty", "data", or None if the banner template hasn't been
        calibrated -- callers should fall back to OCR-based
        classify_table_region in that case."""
        if not auto_detect.variant_paths("no_data_banner"):
            return None
        region = (win.left, win.top, win.width, win.height)
        no_data_box, _ = auto_detect._locate_box("no_data_banner", lambda *a, **k: None, region=region)
        return "empty" if no_data_box is not None else "data"

    def _read_result(self, cnpj: str, win) -> str:
        """OCR-classify the results table for the search already triggered on `win`.
        Caller is responsible for having waited long enough beforehand."""
        rx1, ry1, rx2, ry2 = self._locate_region_box(win, "table_row_region")

        before = self._pre_search_snapshot.get((win.left, win.top))
        if before is not None:
            for _ in range(4):
                after = ocr_utils.grab_region_gray(rx1, ry1, rx2, ry2)
                if ocr_utils.region_changed(before, after):
                    break
                self.log(f"   [{cnpj}] (debug) tabela ainda igual à busca anterior -- "
                         f"aguardando a página atualizar...")
                time.sleep(1.0)
            # Not blocking indefinitely: if it genuinely never changes (e.g. two
            # consecutive records that both happen to have no data, which render
            # pixel-identical), fall through and classify whatever's there once
            # this bounded wait is spent, same as before this check existed.

        if auto_detect.variant_paths("no_data_banner"):
            # Don't trust "banner not found" on the very first look -- a slow
            # page load can still be rendering (spinner, blank transition) at
            # that instant, which looks identical to "not found" and would
            # otherwise get read as "has data" and wrongly eliminate the
            # record. Keep checking for a while before concluding that.
            for attempt in range(1, OCR_RETRIES + 1):
                image_kind = self._classify_table_by_image(win)
                if image_kind == "empty":
                    self.log(f"   [{cnpj}] (debug) tabela lida como VAZIA (reconhecida por imagem).")
                    return STATUS_KEEP
                if attempt < OCR_RETRIES:
                    self.log(f"   [{cnpj}] (debug) aviso de vazio ainda não apareceu na tela "
                             f"(tentativa {attempt}/{OCR_RETRIES}) -- pode estar carregando. "
                             f"Aguardando mais um pouco...")
                    time.sleep(1.5)
            self.log(f"   [{cnpj}] (debug) tabela lida como COM DADOS (aviso de vazio não "
                     f"reconhecido na tela após {OCR_RETRIES} tentativas).")
            return STATUS_ELIMINATE

        best_text = ""
        for attempt in range(1, OCR_RETRIES + 1):
            # Fast (fewer OCR combinations) on the first, most-likely-to-succeed
            # attempt; fall back to the thorough sweep only once it's proven harder.
            kind, text = ocr_utils.classify_table_region(rx1, ry1, rx2, ry2, fast=(attempt == 1))
            if len(text) > len(best_text):
                best_text = text
            if kind == "empty":
                self.log(f"   [{cnpj}] (debug) tabela lida como VAZIA. Texto OCR: '{text}'")
                return STATUS_KEEP
            if kind == "invalid_input":
                self.log(f"   [{cnpj}] (debug) CEP/Número inválido ou vazio nessa linha da planilha. "
                         f"Texto OCR: '{text}'")
                return STATUS_INVALID_INPUT
            if kind == "data":
                if self._looks_like_watermark_noise(text):
                    self.log(f"   [{cnpj}] (debug) tentativa {attempt}/{OCR_RETRIES} leu 'com dados', mas "
                             f"o texto parece ser a marca d'água da sessão, não dado real da tabela "
                             f"(tentativa {attempt}/{OCR_RETRIES}). Texto OCR: '{text}'. Aguardando a "
                             f"marca d'água se mover e tentando de novo...")
                    # Don't give up on the FIRST watermark-looking read -- the watermark
                    # drifts across the page over time (see auto_detect.py's CONFIDENCE
                    # comment), so waiting and re-reading often gets a clean shot at the
                    # real content within the same search, instead of burning a whole
                    # extra reprocessing pass later for something a few more seconds
                    # here would have resolved.
                    time.sleep(1.5)
                    continue
                self.log(f"   [{cnpj}] (debug) tabela lida como COM DADOS. Texto OCR: '{text}'")
                self._data_signatures.append(self._clean_for_signature(text))
                return STATUS_ELIMINATE
            self.log(f"   [{cnpj}] Tabela ainda sem texto legível (tentativa {attempt}/{OCR_RETRIES}), "
                     f"melhor leitura: '{text}'. Aguardando mais um pouco...")
            time.sleep(1.5)

        self.log(f"   [{cnpj}] Falha final de OCR (marca d'água persistente ou tabela ilegível). "
                 f"Última leitura: '{best_text}'")
        # A persistently illegible table is also what you'd see if the page's
        # internal layout shifted (scroll, reflow, zoom) without the WINDOW itself
        # moving -- the click-position cache is keyed by window position, so it
        # wouldn't notice that on its own. Invalidate it so the next attempt
        # re-detects every position fresh instead of continuing to trust
        # coordinates that may no longer be right.
        self._invalidate_click_cache(win)
        return STATUS_ERROR

    def _process_one(self, cnpj: str, win) -> str:
        """Search, wait just long enough for the page to start responding, then
        read. Doesn't need to wait for the FULL page load here -- _read_result
        already retries (pre-search snapshot diff, then banner-recognition
        attempts) until the result actually shows up, so a short initial wait
        followed by those retries is faster in the common case than blindly
        waiting out a fixed delay before even looking."""
        self._start_search(cnpj, win)
        time.sleep(self.config.get("wait_after_search_seconds", 1.0))
        return self._read_result(cnpj, win)

    def _ensure_ready(self):
        """Get a focused, logged-in window. Returns None (and pauses/notifies) if not ready."""
        win = self._get_window()
        if win is None:
            self.log("Janela do Citrix/site não encontrada. Pausando automaticamente.")
            self.pause()
            self.on_logout()
            return None

        citrix_utils.focus_window(win)
        time.sleep(0.3)

        if not self._is_logged_in(win):
            self._consecutive_login_failures += 1
            self.log(f"Aviso: não encontrei o botão Pesquisar na tela "
                     f"({self._consecutive_login_failures}/{MAX_LOGIN_RETRIES}). "
                     f"Pode ter deslogado ou a página não carregou.")
            if self._consecutive_login_failures >= MAX_LOGIN_RETRIES:
                self.log("Pausando automaticamente para você relogar.")
                self.pause()
                self.on_logout()
            else:
                time.sleep(2)
            return None

        self._consecutive_login_failures = 0
        return win

    # ---------- main loop ----------
    def run(self):
        total = len(self.records)
        self.log(f"Iniciando processamento de {total} registros (a partir do item {self.index + 1}).")

        while self.index < total:
            self._check_scheduled_stop()
            if self._stop_event.is_set():
                if not self._scheduled_stop_triggered:
                    self.log("Parado pelo usuário.")
                break

            self._pause_event.wait()
            if self._stop_event.is_set():
                break

            cnpj = self.cnpj_list[self.index]

            win = self._ensure_ready()
            if win is None:
                continue

            try:
                status = self._process_one(cnpj, win)
            except Exception as exc:
                self.log(f"   [{cnpj}] Erro inesperado: {exc}")
                status = STATUS_ERROR

            self.results[cnpj] = status
            self.log(f"   [{cnpj}] -> {status.upper()}")
            self.index += 1
            self._save_checkpoint()
            self.on_progress(self.index, total, cnpj, status)

            delay = self.config.get("delay_between_cnpjs_seconds", 0.7)
            time.sleep(delay + random.uniform(0, 0.5))

        finished_all = self.index >= total
        if finished_all and not self._stop_event.is_set():
            self._retry_errors()

        self.log("Processamento finalizado." if finished_all else "Processamento interrompido.")
        return self.results

    def _retry_errors(self):
        """Second pass: give records that errored out one more shot before calling it done."""
        error_cnpjs = [c for c, s in self.results.items() if s == STATUS_ERROR]
        if not error_cnpjs:
            return

        self.log(f"\nReprocessando {len(error_cnpjs)} registro(s) que deram erro na primeira passada...")
        for cnpj in error_cnpjs:
            self._check_scheduled_stop()
            if self._stop_event.is_set():
                break
            self._pause_event.wait()
            if self._stop_event.is_set():
                break

            win = self._ensure_ready()
            if win is None:
                self.log("Não foi possível continuar a nova tentativa (sessão indisponível). "
                         "Os itens restantes continuam marcados como erro.")
                break

            try:
                status = self._process_one(cnpj, win)
            except Exception as exc:
                self.log(f"   [{cnpj}] Erro na nova tentativa: {exc}")
                status = STATUS_ERROR

            self.results[cnpj] = status
            self.log(f"   [{cnpj}] (nova tentativa) -> {status.upper()}")
            self._save_checkpoint()

            delay = self.config.get("delay_between_cnpjs_seconds", 0.7)
            time.sleep(delay + random.uniform(0, 0.5))
