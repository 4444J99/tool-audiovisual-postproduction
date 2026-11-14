# Execute DAT: resolve automation, events, macros, modulation and Solo once at the start of every frame.
def onFrameStart(frame):
    rack = parent()
    op('td_resolve').module.tick(rack)
    return
