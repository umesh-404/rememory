"""rememory desktop app: system-tray control + dashboard window.

    main.py     tray icon, owns the main thread, launches the dashboard
    window.py   the dashboard window (separate process; see its docstring)
    backend.py  every action both surfaces can perform
    icon.py     the tray image, drawn in code
    ui/         the dashboard front-end (HTML/CSS/JS, no external assets)
"""
