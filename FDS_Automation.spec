# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['menu.py'],
    pathex=[],
    binaries=[],
    # EULA embutida no exe: a tela de aceite sempre tem o texto integral,
    # mesmo que o EULA.txt seja apagado da pasta de distribuicao.
    datas=[('EULA.txt', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # numpy/PIL sao dependencias OPCIONAIS do openpyxl que nao usamos
    # (tipos numpy em celulas / imagens em planilhas). Sem elas o exe
    # volta de 35MB para ~13MB.
    excludes=['numpy', 'PIL', 'lxml'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='FDS_Automation',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
