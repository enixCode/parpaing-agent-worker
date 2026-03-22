// Parse job.json and output shell-sourceable variables.
// Builds CLI command dynamically from engine config.
// Usage: node parse-job.js <job.json> <output.sh>

const fs = require('fs');

const jobPath = process.argv[2];
const outPath = process.argv[3];

if (!jobPath || !outPath) {
  console.error('Usage: node parse-job.js <job.json> <output.sh>');
  process.exit(1);
}

const c = JSON.parse(fs.readFileSync(jobPath, 'utf8'));
const engine = c.engine || {};

function shellEscape(s) {
  if (s === null || s === undefined || s === '') return "''";
  let str = String(s).replace(/\0/g, '');
  if (str.length > 100000) {
    process.stderr.write('Warning: shell argument truncated to 100000 chars\n');
    str = str.substring(0, 100000);
  }
  return "'" + str.replace(/'/g, "'\\''") + "'";
}

// Build CLI args from engine config
const args = [];

// Static args (e.g. --verbose, --full-auto, run)
for (const a of (engine.static_args || [])) {
  args.push(a);
}

// Prompt (positional if prompt_flag is empty, flagged otherwise)
const promptFlag = engine.prompt_flag;
if (promptFlag) {
  args.push(promptFlag);
}
args.push(c.prompt);

// Mapped flags from job config
const flagMap = engine.flag_map || {};
const listJoin = engine.list_join || {};

for (const [configKey, cliFlag] of Object.entries(flagMap)) {
  const value = c[configKey];
  if (value === null || value === undefined) continue;
  if (Array.isArray(value)) {
    const sep = listJoin[configKey] || ',';
    args.push(cliFlag);
    args.push(value.join(sep));
  } else if (typeof value === 'boolean') {
    if (value) args.push(cliFlag);
  } else {
    args.push(cliFlag);
    args.push(typeof value === 'number' && Number.isInteger(value) && configKey.includes('budget')
      ? value.toFixed(2) : String(value));
  }
}

// Build bash array declaration for ARGS
const argsLine = 'ENGINE_ARGS=(' + args.map(a => shellEscape(a)).join(' ') + ')';

const lines = [
  'ENGINE_BINARY=' + shellEscape(engine.binary || 'claude'),
  'ENGINE_ID=' + shellEscape(engine.id || 'claude-code'),
  argsLine,
  'PROMPT=' + shellEscape(c.prompt),
  'DRY_RUN=' + shellEscape(c.dry_run ? '1' : ''),
  'OUTPUT_MODE=' + shellEscape(engine.output_mode || 'stdout'),
  'OUTPUT_PATH=' + shellEscape(engine.output_path || ''),
];

fs.writeFileSync(outPath, lines.join('\n') + '\n');
