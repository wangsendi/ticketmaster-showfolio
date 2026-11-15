'use strict';
const {cloakModule,CLOAK_VERSION} = require('./browser.cjs');
(async()=>{
  const {ensureBinary} = await cloakModule();
  const executable=await ensureBinary(undefined,CLOAK_VERSION);
  process.stdout.write(`CloakBrowser ${CLOAK_VERSION} ready: ${executable}\n`);
})().catch(error=>{process.stderr.write(`CloakBrowser installation failed (${error.name})\n`);process.exitCode=1;});
