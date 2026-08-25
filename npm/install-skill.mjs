#!/usr/bin/env node
/**
 * Install the veripp agent skill.
 *
 *   npx veripp-skill                 into ./.claude/skills/veripp
 *   npx veripp-skill --global        into ~/.claude/skills/veripp
 *   npx veripp-skill --dir <path>    into <path>/veripp
 *
 * This installs the *skill* -- the instructions an agent follows. It does not
 * install the verifier, which is a Python program that needs ESBMC. The skill
 * carries a script that works out how to get those, and says what it will cost
 * before doing anything.
 */

import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE = resolve(HERE, "..", "skills", "veripp");
const EXIT_OK = 0, EXIT_ERROR = 1, EXIT_USAGE = 2;

const USAGE = `veripp-skill — install the veripp agent skill

  npx veripp-skill                 ./.claude/skills/veripp   (this project)
  npx veripp-skill --global        ~/.claude/skills/veripp   (every project)
  npx veripp-skill --dir <path>    <path>/veripp

Options
  --global, -g     install for your user rather than this project
  --dir <path>     install into a directory you choose
  --force, -f      overwrite an existing installation
  --dry-run, -n    say what would happen, change nothing
  --help, -h       this message

The skill tells an agent how to drive veripp. It is not the verifier: that is
a Python program needing ESBMC, and the skill's own install.sh reports what
getting it would cost before touching anything.
`;

function parse(argv) {
  const options = { target: null, global: false, force: false, dryRun: false };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") return { help: true };
    else if (arg === "--global" || arg === "-g") options.global = true;
    else if (arg === "--force" || arg === "-f") options.force = true;
    else if (arg === "--dry-run" || arg === "-n") options.dryRun = true;
    else if (arg === "--dir") {
      options.target = argv[++i];
      if (!options.target) return { error: "--dir needs a path" };
    } else if (arg.startsWith("--dir=")) options.target = arg.slice(6);
    else return { error: `unknown option: ${arg}` };
  }
  return options;
}

function destinationFor(options) {
  if (options.target) return resolve(options.target, "veripp");
  const base = options.global ? homedir() : process.cwd();
  return join(base, ".claude", "skills", "veripp");
}

function main() {
  const options = parse(process.argv.slice(2));
  if (options.help) { process.stdout.write(USAGE); return EXIT_OK; }
  if (options.error) {
    process.stderr.write(`veripp-skill: ${options.error}\n\nRun with --help.\n`);
    return EXIT_USAGE;
  }

  // A published package that cannot find its own payload is broken in a way
  // worth saying plainly, rather than creating an empty directory.
  if (!existsSync(join(SOURCE, "SKILL.md"))) {
    process.stderr.write(
      `veripp-skill: the packaged skill is missing (looked in ${SOURCE}).\n` +
      "This is a packaging bug; please report it at\n" +
      "https://github.com/gfabbretti8/veripp/issues\n");
    return EXIT_ERROR;
  }

  const destination = destinationFor(options);

  if (existsSync(destination) && !options.force) {
    process.stderr.write(
      `veripp-skill: ${destination} already exists.\n` +
      "  Re-run with --force to overwrite it.\n");
    return EXIT_ERROR;
  }

  const files = readdirSync(SOURCE);
  if (options.dryRun) {
    process.stdout.write(
      `Would install ${files.length} file(s) into ${destination}:\n` +
      files.map((f) => `  ${f}\n`).join(""));
    return EXIT_OK;
  }

  try {
    mkdirSync(dirname(destination), { recursive: true });
    cpSync(SOURCE, destination, { recursive: true });
  } catch (error) {
    process.stderr.write(`veripp-skill: could not write ${destination}\n  ${error.message}\n`);
    return EXIT_ERROR;
  }

  // Copying can succeed and still produce something unusable, so check the
  // one file that makes this a skill at all.
  const installed = join(destination, "SKILL.md");
  if (!existsSync(installed) || statSync(installed).size === 0) {
    process.stderr.write(`veripp-skill: ${installed} is missing or empty after install\n`);
    return EXIT_ERROR;
  }

  const name = (readFileSync(installed, "utf8").match(/^name:\s*(\S+)/m) || [])[1] || "veripp";
  process.stdout.write(
    `Installed the ${name} skill into ${destination}\n\n` +
    (options.global
      ? "Available in every project.\n"
      : "Available in this project. Use --global for every project.\n") +
    "\nNext:\n" +
    "  1. Restart your agent session so it picks the skill up.\n" +
    "  2. Ask it to verify a C or C++ function -- it will reach for veripp.\n" +
    "\nThe skill drives veripp; it is not the verifier. If veripp is not\n" +
    "installed, the skill runs this and reports what it would cost first:\n" +
    `  ${join(destination, "install.sh")}\n`);
  return EXIT_OK;
}

process.exit(main());
