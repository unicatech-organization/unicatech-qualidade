"""Simple GUI to drive the CNPJ-checking automation against the Citrix-hosted app."""
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import keyboard
import pyautogui
import pystray
from PIL import Image, ImageDraw

import auto_detect
import automation
import calibration
import io_utils

STATUS_TRAY_COLORS = {
    "mantido": (0, 170, 0, 255),      # verde
    "eliminado": (240, 200, 0, 255),  # amarelo
    "erro": (210, 30, 30, 255),       # vermelho
}
TRAY_IDLE_COLOR = (150, 150, 150, 255)  # cinza


def _make_dot_image(color, size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse((margin, margin, size - margin, size - margin), fill=color)
    return img

pyautogui.FAILSAFE = True  # jogue o mouse pro canto superior-esquerdo da tela para abortar na hora
pyautogui.PAUSE = 0.05

OUTPUT_DIR = Path(__file__).parent / "output"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Automação Vivo Qualidade - Checagem de CNPJ")
        self.geometry("760x560")

        self.log_queue = queue.Queue()
        self.hotkey_queue = queue.Queue()
        self.runner = None
        self.runner_thread = None
        self.records = []
        self.input_path = None
        self.is_paused = False

        self._build_ui()
        self._register_hotkeys()
        self._setup_tray_icon()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._drain_log_queue)
        self.after(100, self._drain_hotkey_queue)

    # ---------------- system tray ----------------
    def _setup_tray_icon(self):
        self._tray_images = {status: _make_dot_image(color) for status, color in STATUS_TRAY_COLORS.items()}
        self._tray_images["idle"] = _make_dot_image(TRAY_IDLE_COLOR)
        self.tray_icon = pystray.Icon(
            "automacao_vivo",
            self._tray_images["idle"],
            "Automação Vivo Qualidade",
            menu=pystray.Menu(
                pystray.MenuItem("Mostrar janela", lambda icon, item: self.after(0, self._tray_show)),
                pystray.MenuItem("Sair", lambda icon, item: self.after(0, self._on_close)),
            ),
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _tray_show(self):
        self.deiconify()
        self.lift()

    def _on_close(self):
        try:
            self.tray_icon.stop()
        except Exception:
            pass
        self.destroy()

    # ---------------- UI ----------------
    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Button(top, text="Testar calibração automática", command=self._on_auto_calibrate).grid(
            row=0, column=0, padx=4)
        ttk.Button(top, text="1) Calibrar manualmente", command=self._on_calibrate).grid(row=0, column=1, padx=4)
        ttk.Button(top, text="2) Upar lista (Excel)", command=self._on_upload).grid(row=0, column=2, padx=4)
        self.start_btn = ttk.Button(top, text="3) Iniciar", command=self._on_start, state="disabled")
        self.start_btn.grid(row=0, column=3, padx=4)
        self.pause_btn = ttk.Button(top, text="Pausar", command=self._on_pause_resume, state="disabled")
        self.pause_btn.grid(row=0, column=4, padx=4)
        self.stop_btn = ttk.Button(top, text="Parar", command=self._on_stop, state="disabled")
        self.stop_btn.grid(row=0, column=5, padx=4)

        schedule = ttk.Frame(self, padding=(10, 0))
        schedule.pack(fill="x")
        ttk.Label(schedule, text="Desligar automaticamente às (HH:MM, opcional):").pack(side="left")
        self.stop_at_var = tk.StringVar()
        ttk.Entry(schedule, textvariable=self.stop_at_var, width=8).pack(side="left", padx=(6, 0))
        ttk.Label(schedule, text="  (salva o progresso e retoma do mesmo ponto depois)",
                  foreground="gray").pack(side="left")

        info = ttk.Frame(self, padding=(10, 0))
        info.pack(fill="x")
        self.file_label = ttk.Label(info, text="Nenhum arquivo carregado.")
        self.file_label.pack(anchor="w")

        prog_frame = ttk.Frame(self, padding=10)
        prog_frame.pack(fill="x")
        self.progress = ttk.Progressbar(prog_frame, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x")
        self.progress_label = ttk.Label(prog_frame, text="0 / 0")
        self.progress_label.pack(anchor="e")

        stats_frame = ttk.Frame(self, padding=(10, 0))
        stats_frame.pack(fill="x")
        self.kept_var = tk.StringVar(value="Mantido: 0 (0%)")
        self.eliminated_var = tk.StringVar(value="Eliminado: 0 (0%)")
        self.error_var = tk.StringVar(value="Erro: 0 (0%)")
        ttk.Label(stats_frame, textvariable=self.kept_var).pack(side="left", padx=(0, 16))
        ttk.Label(stats_frame, textvariable=self.eliminated_var).pack(side="left", padx=(0, 16))
        ttk.Label(stats_frame, textvariable=self.error_var).pack(side="left")

        console_frame = ttk.Frame(self, padding=10)
        console_frame.pack(fill="both", expand=True)
        ttk.Label(console_frame, text="Console:").pack(anchor="w")
        self.console = scrolledtext.ScrolledText(console_frame, state="disabled", wrap="word", height=20)
        self.console.pack(fill="both", expand=True)

        hint = ("Dica: jogue o mouse para o canto superior-esquerdo da tela a qualquer momento "
                "para abortar imediatamente (failsafe do pyautogui). Atalhos: F8 pausa/retoma, "
                "F9 para e já salva o progresso.")
        ttk.Label(self, text=hint, foreground="gray", padding=(10, 0, 10, 10)).pack(anchor="w")

    # ---------------- logging ----------------
    def log(self, msg: str):
        self.log_queue.put(msg)

    def _drain_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.console.configure(state="normal")
                ts = datetime.now().strftime("%H:%M:%S")
                self.console.insert("end", f"[{ts}] {msg}\n")
                self.console.see("end")
                self.console.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._drain_log_queue)

    # ---------------- actions ----------------
    def _on_calibrate(self):
        if self.runner_thread and self.runner_thread.is_alive():
            messagebox.showwarning("Aviso", "Pare a automação antes de calibrar novamente.")
            return

        def worker():
            ok = calibration.run_calibration(log=self.log)
            if ok:
                self.log("Calibração concluída com sucesso.")
            else:
                self.log("Calibração não foi concluída.")

        threading.Thread(target=worker, daemon=True).start()

    def _on_auto_calibrate(self):
        if self.runner_thread and self.runner_thread.is_alive():
            messagebox.showwarning("Aviso", "Pare a automação antes de recalibrar.")
            return

        def worker():
            ok = auto_detect.try_auto_calibrate(log=self.log)
            if ok:
                self.log("Calibração automática funcionou! Já pode iniciar.")
            else:
                self.log("Calibração automática não encontrou tudo. Rode a calibração manual (passo 1).")

        threading.Thread(target=worker, daemon=True).start()

    def _on_upload(self):
        path = filedialog.askopenfilename(
            title="Selecione a lista (Excel com colunas CEP, Número e CNPJ)",
            filetypes=[("Excel files", "*.xlsx *.xls")],
        )
        if not path:
            return
        try:
            records, columns_used = io_utils.load_search_list_from_excel(path)
        except Exception as exc:
            messagebox.showerror("Erro ao ler arquivo", str(exc))
            return

        self.records = records
        self.input_path = path
        cnpj_col = columns_used["cnpj"] or "(nenhuma -- usando CEP+Número como identificador)"
        self.file_label.config(
            text=f"{Path(path).name} — CEP: '{columns_used['cep']}', Número: '{columns_used['numero']}', "
                 f"CNPJ: '{cnpj_col}' — {len(records)} registros carregados."
        )
        self.log(f"Lista carregada: {len(records)} registros "
                 f"(CEP: '{columns_used['cep']}', Número: '{columns_used['numero']}', CNPJ: '{cnpj_col}').")
        self.progress.configure(maximum=len(records), value=0)
        self.progress_label.config(text=f"0 / {len(records)}")
        self.kept_var.set("Mantido: 0 (0%)")
        self.eliminated_var.set("Eliminado: 0 (0%)")
        self.error_var.set("Erro: 0 (0%)")
        self.start_btn.configure(state="normal")

    def _on_start(self):
        config = calibration.load_config()
        if config is None:
            messagebox.showwarning("Calibração pendente", "Rode a calibração (passo 1) antes de iniciar.")
            return
        if not self.records:
            messagebox.showwarning("Lista pendente", "Upe uma lista (passo 2) antes de iniciar.")
            return

        stop_at_text = self.stop_at_var.get().strip()
        stop_at_time = None
        if stop_at_text:
            try:
                stop_at_time = datetime.strptime(stop_at_text, "%H:%M").time()
            except ValueError:
                messagebox.showwarning("Horário inválido", "Use o formato HH:MM (ex.: 20:00) ou deixe em branco.")
                return

        signature = io_utils.file_signature(self.input_path)
        self.runner = automation.AutomationRunner(
            config=config,
            records=self.records,
            log=self.log,
            on_progress=self._on_progress,
            on_logout=self._on_logout_detected,
            input_signature=signature,
            stop_at_time=stop_at_time,
        )
        if stop_at_time:
            self.log(f"Parada automática agendada para {stop_at_time.strftime('%H:%M')}.")

        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="Pausar")
        self.stop_btn.configure(state="normal")
        self.is_paused = False
        self.tray_icon.icon = self._tray_images["idle"]

        self.log("Minimizando... a automação começa a mexer na tela em 5 segundos.")
        self.iconify()
        self.after(5000, self._launch_runner_thread)

    def _launch_runner_thread(self):
        def worker():
            results = self.runner.run()
            self._finish_run(results)

        self.runner_thread = threading.Thread(target=worker, daemon=True)
        self.runner_thread.start()

    def _on_pause_resume(self):
        if not self.runner:
            return
        if self.is_paused:
            self.runner.resume()
            self.pause_btn.configure(text="Pausar")
            self.is_paused = False
        else:
            self.runner.pause()
            self.pause_btn.configure(text="Retomar")
            self.is_paused = True

    def _on_stop(self):
        if not self.runner:
            return
        if messagebox.askyesno("Confirmar", "Parar a automação? O progresso fica salvo e você pode retomar depois."):
            self.runner.stop()
            self.stop_btn.configure(state="disabled")

    # ---------------- global hotkeys ----------------
    def _register_hotkeys(self):
        try:
            keyboard.add_hotkey("f8", lambda: self.hotkey_queue.put("pause"))
            keyboard.add_hotkey("f9", lambda: self.hotkey_queue.put("stop"))
        except Exception as exc:
            self.log(f"Aviso: não foi possível registrar os atalhos F8/F9 ({exc}).")

    def _drain_hotkey_queue(self):
        try:
            while True:
                action = self.hotkey_queue.get_nowait()
                if action == "pause":
                    self._on_pause_resume()
                elif action == "stop":
                    self._hotkey_stop()
        except queue.Empty:
            pass
        self.after(100, self._drain_hotkey_queue)

    def _hotkey_stop(self):
        if not self.runner:
            return
        self.log("Atalho F9 pressionado -- parando e salvando o progresso.")
        self.runner.stop()
        self.stop_btn.configure(state="disabled")

    def _on_logout_detected(self):
        self.pause_btn.configure(text="Retomar")
        self.is_paused = True
        self.log("*** Ação necessária: verifique/relogue no Citrix e clique em 'Retomar'. ***")
        try:
            self.bell()
        except Exception:
            pass

    def _on_progress(self, index, total, cnpj, status):
        self.progress.configure(value=index)
        self.progress_label.config(text=f"{index} / {total}  (último: {cnpj} -> {status})")
        self._update_stats()
        tray_img = self._tray_images.get(status)
        if tray_img is not None:
            self.tray_icon.icon = tray_img

    def _update_stats(self):
        if not self.runner:
            return
        results = self.runner.results
        total = len(self.runner.records)
        kept = sum(1 for s in results.values() if s == "mantido")
        eliminated = sum(1 for s in results.values() if s == "eliminado")
        errors = sum(1 for s in results.values() if s == "erro")

        def pct(n):
            return (n / total * 100) if total else 0

        self.kept_var.set(f"Mantido: {kept} ({pct(kept):.1f}%)")
        self.eliminated_var.set(f"Eliminado: {eliminated} ({pct(eliminated):.1f}%)")
        self.error_var.set(f"Erro: {errors} ({pct(errors):.1f}%)")

    def _finish_run(self, results):
        self.start_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled")
        self.stop_btn.configure(state="disabled")
        self._update_stats()
        self.tray_icon.icon = self._tray_images["idle"]

        OUTPUT_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = OUTPUT_DIR / f"resultado_{ts}.xlsx"
        records = self.runner.records if self.runner else []
        io_utils.write_output_excel(records, results, str(out_path))
        self.log(f"Arquivo de saída gerado: {out_path}")

        errors_path = OUTPUT_DIR / f"erros_{ts}.xlsx"
        if io_utils.write_errors_excel(records, results, str(errors_path)):
            self.log(f"Planilha de erros gerada: {errors_path}")

        kept = sum(1 for s in results.values() if s == "mantido")
        eliminated = sum(1 for s in results.values() if s == "eliminado")
        errors = sum(1 for s in results.values() if s == "erro")
        invalid = sum(1 for s in results.values() if s == "cep_ou_numero_invalido")
        self.log(f"Resumo: {kept} mantidos, {eliminated} eliminados, {errors} com erro (após nova tentativa), "
                 f"{invalid} com CEP/Número inválido ou vazio na planilha.")

        if self.runner and self.runner.index >= len(self.runner.records):
            self.runner.clear_checkpoint()


if __name__ == "__main__":
    App().mainloop()
