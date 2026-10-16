declare module "virtual:emoji-data" {
  /** [group name, "emoji\tname\temoji\tname…"] — see emojiData() in vite.config.ts. */
  const groups: [string, string][];
  export default groups;
}
