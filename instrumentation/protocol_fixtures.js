'use strict';
(function () {
  Java.perform(function () {
    const Json = Java.use('a3.b');
    const Sign = Java.use('x2.f');
    const Aes = Java.use('x2.a');
    const Base64 = Java.use('x2.b');
    const Rsa = Java.use('x2.e');
    const TreeMap = Java.use('java.util.TreeMap');
    const StringType = Java.use('java.lang.String');
    const Native = Java.use('com.jingcai.apps.qualitydev.utils.NativeUtil');
    const context = Java.use('android.app.ActivityThread').currentApplication().getApplicationContext();
    const requestIv = String(Native.getNetSignReIV(context));
    const responseIv = String(Native.getNetSignPlIV(context));
    const publicKey = Rsa.b(Native.getNetSignModuleKey(context), Native.getNetSignExponent(context));
    const key = 'abcdefghijklmnop';
    const cases = [
      { version: '5.0.0', timestamp: '1700000000123', uid: '42', token: 'example-token' },
      { z: '\u6c49\u5b57<>&=\'"/\u2028\u2029', a: null, timestamp: '1700000000124', empty: '' },
      { '\ue000': 'private-use', '\ud83d\ude00': 'astral', a: 'x', timestamp: '1700000000125' },
      { page: '2', newline: 'a\n\tb\b', timestamp: '1700000000126' }
    ];
    function emit(value) {
      console.log('[DM-FIXTURE] ' + JSON.stringify(value).replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029'));
    }
    for (const params of cases) {
      const map = TreeMap.$new();
      for (const name of Object.keys(params)) map.put(name, params[name] === null ? '' : String(params[name]));
      const serialized = String(Json.e(map));
      const signToken = String(Sign.d(map));
      map.put('signToken', signToken);
      const requestPlaintext = String(Json.e(map));
      const aesCiphertext = String(Aes.b(requestPlaintext, key, requestIv));
      const rsaCiphertext = String(Rsa.a(publicKey, key));
      const bytes = StringType.$new(aesCiphertext + rsaCiphertext).getBytes('UTF-8');
      const wireValue = String(Base64.d(bytes));
      const responsePlaintext = '{"code":100,"data":{"probe":true}}';
      const responseCiphertext = String(Aes.b(responsePlaintext, key, responseIv));
      emit({ kind: 'protocol', params: params, serialized: serialized, signToken: signToken,
        requestPlaintext: requestPlaintext, key: key, requestIv: requestIv,
        aesCiphertext: aesCiphertext, rsaCiphertext: rsaCiphertext, wireValue: wireValue,
        responseIv: responseIv, responsePlaintext: responsePlaintext, responseCiphertext: responseCiphertext });
    }
    for (const size of [0, 1, 2, 3, 44, 45, 46, 90]) {
      emit({ kind: 'base64', size: size, inputByte: 65,
        encoded: String(Base64.d(Java.array('byte', Array(size).fill(65)))) });
    }
  });
})();
