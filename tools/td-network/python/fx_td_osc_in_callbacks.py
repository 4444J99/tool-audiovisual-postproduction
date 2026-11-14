# OSC In DAT callbacks. All logic lives in the td_osc module DAT; this only forwards.
def onReceiveOSC(dat, rowIndex, message, byteData, timeStamp, address, args, peer):
    rack = parent()
    op('td_osc').module.link(rack).receive(address, list(args))
    return


def onReceiveOSCBundle(dat, rowIndex, message, byteData, timeStamp, address, args, peer):
    return
