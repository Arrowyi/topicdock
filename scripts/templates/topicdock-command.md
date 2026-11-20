---
description: "TopicDock topic memory entry — with no arguments, list available topics and guide mounting/creation; with an argument, mount that topic directly. Keywords: topic memory / 主题记忆, continue <id|index> / 继续 <id|序号>, delete <index> / 删 <序号>."
---
<!-- topicdock:command-managed -->

# /topicdock: TopicDock Topic Mount

The user invoked the `/topicdock` command; the text appended after the command is: $ARGUMENTS

## 1. Check the arguments first

- If arguments follow the command and look like `<id|序号>` (a topic id such as `auth-refactor`, or a list index such as `3`), **mount directly**: call `td use <id|序号> --trace auto`, treat the returned ledger body as the current context, then continue the conversation per the `topicdock` skill's three-pillar workflow (Read / Trace / Write — see the skill's `SKILL.md`); this template does not repeat the workflow details.
- Otherwise (no appended arguments, or the argument does not look like `<id|序号>`), follow the **no-argument guidance flow** below.

## 2. No-argument guidance flow

Call `td use` (without arguments) and present one of three situations to the user based on the result:

1. **0 topics in the store**: tell the user there is no topic yet and ask what subject this session should work on; then, per the `SKILL.md` Write workflow, save the first topic with `base_hop: null` (`td save`), and echo the ⚓ line at the end of the reply after creation succeeds.
2. **Exactly 1 topic in the store**: show no list; mount that topic directly and display the ledger body.
3. **2 or more topics in the store**: show the first page of the list — list rows on top, format `序号 | 标题 | id | 更新时间`; operation hints below the list, in natural language, **converged to the list's actual state** (omit items that do not apply):

   - Always write: "Reply with an index to mount the corresponding topic."
   - Always write: "Reply `删 <序号>` (delete <index>) if you want a topic removed: I will first show a deletion preview, and will execute only after you explicitly reply with that topic's full topic_id." (On receiving `删 <序号>`: first call `td delete <id> --dry-run` and show the preview; after the user explicitly replies with that topic_id, execute `td delete <topic_id> --yes`.)
   - Only when further pages exist: "Reply 'next page' / 『下一页』 to see more, or reply 'all' / 『全部』 to list every topic at once." Do not write this line when there is only one page.
   - Always write: "You may also give keywords directly, and I will list only the matching topics." (On receiving keywords, filter by them and re-list.)

The list is for selection and deletion only. After mounting succeeds, continue per the `SKILL.md` three-pillar workflow.
