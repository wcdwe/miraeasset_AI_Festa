const {spawnSync}=require('child_process');
const fs=require('fs');

const candidates=[
 process.env.PENSION_PYTHON,
 process.platform==='win32'?'.\\.venv\\Scripts\\python.exe':'.venv/bin/python',
 'C:\\Users\\k9905\\anacondaa\\python.exe',
 'C:\\Users\\k9905\\anaconda3\\python.exe',
 'python',
 'python3'
].filter(Boolean);

const args=process.argv.slice(2);
if(!args.length){
 console.error('usage: node integration/run_python.js <script> [...args]');
 process.exit(2);
}
for(const executable of candidates){
 if(executable.includes('\\')&&!fs.existsSync(executable))continue;
 const result=spawnSync(executable,args,{stdio:'inherit'});
 if(!result.error)process.exit(result.status??1);
}
console.error('No working Python executable found. Set PENSION_PYTHON.');
process.exit(1);
