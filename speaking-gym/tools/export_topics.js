#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const { TOPICS } = require(path.join(__dirname, "..", "data.js"));

const categories = Object.values(TOPICS).map((category) => ({
  label: category.label,
  topics: category.items.map((item) => item.q),
}));
const output = path.join(__dirname, "..", "topics.json");
fs.writeFileSync(output, JSON.stringify({ categories }));
console.log(`topics.json 导出完成：${categories.reduce((sum, category) => sum + category.topics.length, 0)} 题`);
