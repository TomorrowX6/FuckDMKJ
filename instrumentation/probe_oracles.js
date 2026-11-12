'use strict';
(function () {
  let attempts = 0;
  function run() {
    if (!globalThis.__dmProbe || !globalThis.__dmProbe.status().ready) {
      if (++attempts < 40) setTimeout(run, 500);
      else console.log('[DM-ORACLE] probe did not become ready');
      return;
    }
    const cases = [
      { version: '5.0.0', timestamp: '1700000000123', uid: '42', token: 'example-token' },
      { z: '\u6c49\u5b57<>&=\'"/\u2028\u2029', a: null, timestamp: '1700000000124', empty: '' },
      { '\ue000': 'private-use', '\ud83d\ude00': 'astral', a: 'x', timestamp: '1700000000125' },
      { page: '2', newline: 'a\n\tb\b', timestamp: '1700000000126' }
    ];
    let promise = Promise.resolve();
    for (const value of cases) promise = promise.then(() => globalThis.__dmProbe.reference(value));
    promise.then(() => console.log('[DM-ORACLE] DONE ' + cases.length))
      .catch(error => console.log('[DM-ORACLE] ERROR ' + error.stack));
  }
  setTimeout(run, 4000);
})();
