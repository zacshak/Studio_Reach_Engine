import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {sql} from './worker.js';

export async function migrateSocials(env,run=sql) {
  const [cols]=await run(env,[['PRAGMA table_info(scrape_tracker)']]);
  if(!cols.some(c=>c[1]==='Socials_Request_ID'))
    await run(env,[['ALTER TABLE scrape_tracker ADD COLUMN Socials_Request_ID TEXT']]);
  // Never reset a request created by the new Send-click path on migration retry.
  const [rows]=await run(env,[[`UPDATE scrape_tracker SET Require_Socials=0,
    Socials_Data=json_object(
      'X',CASE WHEN json_type(Socials_Data,'$.X')='text' THEN json_extract(Socials_Data,'$.X') END,
      'LinkedIn',CASE WHEN json_type(Socials_Data,'$.LinkedIn')='text' THEN json_extract(Socials_Data,'$.LinkedIn') END,
      'Instagram',CASE WHEN json_type(Socials_Data,'$.Instagram')='text' THEN json_extract(Socials_Data,'$.Instagram') END,
      'Discord',CASE WHEN json_type(Socials_Data,'$.Discord')='text' THEN json_extract(Socials_Data,'$.Discord') END,
      'Website',CASE WHEN json_type(Socials_Data,'$.Website')='text' THEN json_extract(Socials_Data,'$.Website') END)
    WHERE Socials_Request_ID IS NULL RETURNING appid`]]);
  return rows.length;
}

if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
  const raw=await readFile(new URL('../.env',import.meta.url),'utf8');
  const values=Object.fromEntries(raw.split(/\r?\n/).filter(l=>l&&!l.startsWith('#')&&l.includes('=')).map(l=>{
    const at=l.indexOf('=');return [l.slice(0,at).trim(),l.slice(at+1).trim().replace(/^['"]|['"]$/g,'')];
  }));
  const env={TURSO_URL:values.TURSO_DATABASE_URL,TURSO_TOKEN:values.TURSO_AUTH_TOKEN};
  if(!env.TURSO_URL||!env.TURSO_TOKEN)throw new Error('Database configuration missing');
  const backup=await sql(env,[['SELECT appid,Require_Socials,Socials_Data FROM scrape_tracker ORDER BY appid']]);
  await mkdir(new URL('../out/',import.meta.url),{recursive:true});
  const name=`socials-before-${Date.now()}.json`;
  await writeFile(new URL('../out/'+name,import.meta.url),JSON.stringify(backup[0]),{flag:'wx'});
  console.log('Previous socials saved locally:',name);
  console.log('Existing records normalized without backfilling requests:',await migrateSocials(env));
}
