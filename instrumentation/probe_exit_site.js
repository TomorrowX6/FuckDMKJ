'use strict';

const seen = new Set();
const observer = Process.attachModuleObserver({
    onAdded(module) {
        if (module.name !== 'libnesec.so' || seen.has(module.base.toString())) return;
        seen.add(module.base.toString());
        console.log('[SITE_PROBE] module=' + module.name + ' base=' + module.base + ' path=' + module.path);
        console.log('[SITE_PROBE] bytes\n' + hexdump(module.base.add(0x2f620), { length: 0xe0, header: false, ansi: false }));
        for (let offset = 0x2f620; offset < 0x2f700; offset += 4) {
            const address = module.base.add(offset);
            try { console.log('[SITE_PROBE] +' + offset.toString(16) + ' ' + Instruction.parse(address)); }
            catch (error) { console.log('[SITE_PROBE] +' + offset.toString(16) + ' ' + error); }
        }
    }
});
