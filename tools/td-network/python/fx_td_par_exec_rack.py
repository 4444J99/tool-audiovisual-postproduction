# Parameter Execute DAT for the rack component itself: selection, transport and the pulse buttons.
def onValueChange(par, prev):
    rack = parent()
    module = op('td_osc').module
    module.link(rack).par_changed('', par.name, par.eval())
    return


def onPulse(par):
    rack = parent()
    module = op('td_osc').module
    module.link(rack).pulse('', par.name)
    return
