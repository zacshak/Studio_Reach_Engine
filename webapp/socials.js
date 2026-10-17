export const SOCIAL_FIELDS = ['X','LinkedIn','Instagram','Discord','Website'];
const domains = {X:['x.com','twitter.com'],LinkedIn:['linkedin.com'],Instagram:['instagram.com'],Discord:['discord.com','discord.gg']};

export function validateSocials(data) {
  if (!data || Array.isArray(data) || typeof data !== 'object' ||
      Object.keys(data).sort().join('|') !== [...SOCIAL_FIELDS].sort().join('|'))
    throw new Error('Socials must contain exactly X, LinkedIn, Instagram, Discord, Website.');
  return Object.fromEntries(SOCIAL_FIELDS.map(key => {
    const value=data[key];
    if(value===null) return [key,null];
    if(typeof value!=='string' || value.length>2048 || value!==value.trim() || /\s/.test(value))
      throw new Error(`${key} must be an HTTP(S) URL or null.`);
    let url;try {url=new URL(value);} catch {throw new Error(`Invalid ${key} URL.`);}
    if(!['http:','https:'].includes(url.protocol) || url.username || url.password || !url.hostname.includes('.'))
      throw new Error(`Invalid ${key} URL.`);
    if(domains[key] && !domains[key].some(host=>url.hostname===host||url.hostname.endsWith('.'+host)))
      throw new Error(`${key} URL has an unsupported domain.`);
    return [key,value];
  }));
}

// A Send click is the request. Keep it even if GitHub dispatch subsequently fails.
// The immutable request ID prevents quota/retry runs from requesting it twice.
export const queueSocialsStatement = () => [
  "UPDATE scrape_tracker SET Require_Socials=1,Socials_Request_ID=lower(hex(randomblob(16))) WHERE Mail_status='Scheduled' AND Socials_Request_ID IS NULL RETURNING appid"
];

export async function saveSocials(env, body, sql) {
  const {appid,request_id}=body;
  if(!Number.isSafeInteger(appid)||appid<=0 || typeof request_id!=='string'|| !/^[a-f0-9]{32}$/.test(request_id))
    throw new Error('A valid appid and request_id are required.');
  const data=validateSocials(body.socials), encoded=JSON.stringify(data);
  const [saved]=await sql(env,[[
    'UPDATE scrape_tracker SET Socials_Data=?,Require_Socials=0 WHERE appid=? AND Require_Socials=1 AND Socials_Request_ID=? RETURNING appid',
    encoded,appid,request_id
  ]]);
  if(!saved.length) {
    const [rows]=await sql(env,[['SELECT Require_Socials,Socials_Request_ID,Socials_Data FROM scrape_tracker WHERE appid=?',appid]]);
    const current=rows[0];
    if(!current || Number(current[0])!==0 || current[1]!==request_id ||
       JSON.stringify(validateSocials(JSON.parse(current[2])))!==encoded)
      throw new Error('Social request is missing, stale, or already completed with different data.');
  }
  return {ok:true,appid,require_socials:false,socials:data};
}

export async function readSocialRequestBody(req) {
  const reader=req.body?.getReader();if(!reader)throw new Error('JSON body required.');
  const chunks=[];let size=0;
  try{for(;;){const {done,value}=await reader.read();if(done)break;size+=value.length;
    if(size>16384)throw new Error('Social payload exceeds 16 KiB.');chunks.push(value);}}
  finally{await reader.cancel();}
  const bytes=new Uint8Array(size);let offset=0;for(const c of chunks){bytes.set(c,offset);offset+=c.length;}
  return JSON.parse(new TextDecoder().decode(bytes));
}
