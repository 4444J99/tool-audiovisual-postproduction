# Parameter Execute DAT for every processor component (fx_*): an edit becomes an OSC control message.
def onValueChange(par, prev):
    rack = parent()
    module = op('td_osc').module
    module.link(rack).par_changed(module.owner_name(rack, par), par.name, par.eval())
    return


def onPulse(par):
    return
