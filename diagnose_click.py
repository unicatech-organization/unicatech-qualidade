"""Diagnostic-only script: does NOT click or type anything, does NOT change
config.json or any calibration file. Just prints out every number involved in
figuring out where the Pesquisar button click lands, so we can see exactly
where the real-time calculation diverges from reality.

Run this with the Citrix window open and visible (same profile/DPI setup you
use for real), then read the printed report.

Rode: python diagnose_click.py
"""
import ctypes

import auto_detect
import calibration
import citrix_utils
import profiles

REGION_ELEMENTS = auto_detect.REGION_ELEMENTS


def _dist(p1, p2):
    return ((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2) ** 0.5


def main():
    print("=== DIAGNÓSTICO: clique do botão Pesquisar ===\n")

    print(f"Perfil de calibração ativo: '{profiles.get_active_profile()}'")
    print(f"  config.json:   {profiles.config_path()}")
    print(f"  templates/:    {profiles.templates_dir()}\n")

    try:
        awareness = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(awareness))
        awareness_names = {0: "DPI-unaware", 1: "System DPI-aware", 2: "Per-Monitor DPI-aware"}
        print(f"DPI awareness deste processo: {awareness.value} "
              f"({awareness_names.get(awareness.value, '?')})")
    except Exception as exc:
        print(f"Não consegui ler o DPI awareness ({exc})")

    try:
        import win32api
        monitors = win32api.EnumDisplayMonitors()
        for i, (hmon, _hdc, rect) in enumerate(monitors):
            try:
                scale = ctypes.c_uint()
                ctypes.windll.shcore.GetScaleFactorForMonitor(hmon.handle, ctypes.byref(scale))
                print(f"  Monitor {i}: rect={rect}, escala reportada={scale.value}%")
            except Exception as exc:
                print(f"  Monitor {i}: rect={rect}, escala não lida ({exc})")
    except Exception as exc:
        print(f"Não consegui enumerar monitores ({exc})")

    config = calibration.load_config()
    if config is None:
        print("\nNenhuma calibração salva nesse perfil -- calibre primeiro (passo 1).")
        return
    print()

    win = citrix_utils.find_window(config["window_title_contains"])
    if win is None:
        print("Não encontrei a janela do Citrix pelo título. Abra-a e rode de novo.")
        return
    print(f"Janela encontrada por título: '{win.title}'")
    print(f"  left={win.left}, top={win.top}, width={win.width}, height={win.height}\n")

    for name in ["cep_field", "numero_field", "pesquisar_button"]:
        if name not in config:
            continue
        raw_point = citrix_utils.absolute_point(win, config[name]["x"], config[name]["y"])
        print(f"[{name}]")
        print(f"  offset salvo no config.json: {config[name]}")
        print(f"  ponto cru recalculado agora (janela.left/top + offset): {raw_point}")

        if auto_detect.has_templates():
            region = (win.left, win.top, win.width, win.height)
            pos = auto_detect._locate_point(name, print, region=region)
            if pos is not None:
                matched = (pos.x, pos.y)
                drift = _dist(matched, raw_point)
                print(f"  posição achada por reconhecimento de imagem agora: {matched}")
                print(f"  DIFERENÇA (cru vs. imagem): {drift:.1f}px "
                      f"{'  <<< acima do limite de 60px, seria REJEITADA' if drift > 60 else ''}")
            else:
                print("  reconhecimento de imagem: não encontrou (sem template ou não bateu na tela)")
        print()

    print("Se a 'DIFERENÇA' do pesquisar_button estiver marcada como REJEITADA acima,")
    print("essa é a causa do clique errado: o sistema está usando o 'ponto cru' em vez")
    print("da posição real encontrada por imagem.")


if __name__ == "__main__":
    main()
