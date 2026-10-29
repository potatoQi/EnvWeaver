// Group selection stays symbolic so collapsed and unloaded rows are included.
export function createSelection() {
  let all = false;
  const groups = new Map();
  const excludedGroups = new Map();
  const selected = new Map();
  const excluded = new Map();
  const base = group => all ? !excludedGroups.has(group) : groups.has(group);
  const sum = values => [...values].reduce((total, value) => total + value, 0);
  const groupEntries = (entries, group) => [...entries.values()].filter(key => key === group).length;

  function clear(checked = false) {
    all = checked;
    groups.clear();
    excludedGroups.clear();
    selected.clear();
    excluded.clear();
  }
  function toggleRow(id, group, checked) {
    selected.delete(id);
    excluded.delete(id);
    if (checked !== base(group)) (checked ? selected : excluded).set(id, group);
  }
  function toggleGroup(group, checked) {
    for (const entries of [selected, excluded]) {
      for (const [id, key] of entries) if (key === group.key) entries.delete(id);
    }
    if (all) {
      if (checked) excludedGroups.delete(group.key);
      else excludedGroups.set(group.key, group.count);
    } else {
      if (checked) groups.set(group.key, group.count);
      else groups.delete(group.key);
    }
  }
  function groupCount(group) {
    if (groups.has(group.key)) groups.set(group.key, group.count);
    if (excludedGroups.has(group.key)) excludedGroups.set(group.key, group.count);
    return Math.max(0, Math.min(group.count,
      (base(group.key) ? group.count : 0) + groupEntries(selected, group.key) - groupEntries(excluded, group.key)));
  }
  return {
    clear, toggleRow, toggleGroup, groupCount,
    has: (id, group) => selected.has(id) || (base(group) && !excluded.has(id)),
    count: total => Math.max(0, (all ? total - sum(excludedGroups.values()) : sum(groups.values())) + selected.size - excluded.size),
    query: () => ({
      include_all: all, ids: [...selected.keys()], exclude_ids: [...excluded.keys()],
      task_groups: [...groups.keys()], exclude_task_groups: [...excludedGroups.keys()],
    }),
  };
}
