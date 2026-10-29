export function readSession(key) {
  try {
    return JSON.parse(sessionStorage.getItem(`envweaver.${key}`));
  } catch {
    return null;
  }
}

export function writeSession(key, value) {
  try {
    if (value === null) sessionStorage.removeItem(`envweaver.${key}`);
    else sessionStorage.setItem(`envweaver.${key}`, JSON.stringify(value));
  } catch {
    // Navigation still works when browser storage is unavailable.
  }
}
