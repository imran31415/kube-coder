/**
 * Generate a "clever" default name for a new build session — adjective +
 * noun + small number. Keeps the New Build flow seamless: user clicks the
 * button, we pre-fill a memorable name they can keep or rename.
 *
 * Avoids collisions with the much smaller in-memory list by appending a
 * random 1-99 suffix. Two simultaneous calls can collide on the suffix but
 * the server's task_id (timestamp + 8 hex) is what disambiguates internally.
 */
const ADJ = [
  'funny', 'curious', 'silent', 'lucky', 'brave', 'cosmic', 'eager',
  'gentle', 'jolly', 'mighty', 'nimble', 'quiet', 'rapid', 'shiny',
  'swift', 'tidy', 'vivid', 'wise', 'zesty', 'sunny', 'spicy', 'plucky',
  'witty', 'happy', 'sleepy', 'glowing', 'breezy', 'frosty', 'amber',
];

const NOUN = [
  'kitty', 'panda', 'otter', 'falcon', 'tiger', 'comet', 'rocket',
  'pixel', 'forest', 'ember', 'meteor', 'wave', 'lantern', 'cipher',
  'beacon', 'puzzle', 'orbit', 'echo', 'glacier', 'kernel', 'phantom',
  'spectre', 'turbine', 'volcano', 'wombat', 'zebra', 'circuit', 'galaxy',
];

function pick<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)];
}

export function randomBuildName(): string {
  const n = 1 + Math.floor(Math.random() * 99);
  return `${pick(ADJ)}-${pick(NOUN)}-${n}`;
}
