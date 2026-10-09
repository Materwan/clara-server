# Vendored: the Sendspin player (`sendspin.js`)

`sendspin.js` is `@sendspin/sendspin-js` 5.0.0 (Apache-2.0, licence in `sendspin-LICENSE.txt`) with its dependencies
(`@noble/ciphers`, `@noble/curves`, `@noble/hashes`, `opus-encdec`) bundled into one ES module, so the music site
loads nothing from elsewhere. It is built once, by hand, and committed:

    npm install @sendspin/sendspin-js@5.0.0 esbuild
    echo "export { SendspinPlayer, loadSendspinClientIdentity } from '@sendspin/sendspin-js';" > entry.js
    npx esbuild entry.js --bundle --format=esm --minify --target=es2020 --legal-comments=none --outfile=sendspin.js

The music site needs `'wasm-unsafe-eval'` (the Opus decoder is WebAssembly) and `media-src data:` (the SDK's silent
keep-alive sound), both set for the music site in `webapi.py`.
