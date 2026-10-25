.pragma library
// Разметка markdown для сообщений в панели чата. Функции живут в замыкании, наружу
// отдаём render() и escapeHtml() (см. конец файла).
var window = {};
/* Небольшой и безопасный рендер markdown для сообщений чата.
 * Умеет: заголовки, абзацы, жирный/курсив/зачёркнутый, код в строке и блоком,
 * маркированные и нумерованные списки (один уровень вложенности по отступу), таблицы,
 * цитаты, горизонтальные линии и ссылки http(s).
 * Весь ввод сначала экранируется, поэтому ответ модели не может подсунуть свою разметку.
 */
(function (global) {
  "use strict";

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Оформление внутри строки, текст уже экранирован.
  function inline(text) {
    var codes = [];
    text = text.replace(/`([^`]+)`/g, function (_, code) {
      codes.push(code);
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    text = text
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return text.replace(/\u0000(\d+)\u0000/g, function (_, i) {
      return "<code>" + codes[Number(i)] + "</code>";
    });
  }

  var TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
  var LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

  function splitRow(line) {
    var row = line.trim();
    if (row.charAt(0) === "|") row = row.slice(1);
    if (row.charAt(row.length - 1) === "|") row = row.slice(0, -1);
    return row.split("|").map(function (cell) { return cell.trim(); });
  }

  function renderTable(lines) {
    var header = splitRow(lines[0]);
    var aligns = splitRow(lines[1]).map(function (cell) {
      var left = cell.charAt(0) === ":", right = cell.charAt(cell.length - 1) === ":";
      return right && left ? "center" : right ? "right" : "";
    });
    function cellHtml(tag, cell, i) {
      var style = aligns[i] ? ' style="text-align:' + aligns[i] + '"' : "";
      return "<" + tag + style + ">" + inline(cell) + "</" + tag + ">";
    }
    var html = '<div class="md-table"><table><thead><tr>' +
      header.map(function (c, i) { return cellHtml("th", c, i); }).join("") + "</tr></thead><tbody>";
    lines.slice(2).forEach(function (line) {
      html += "<tr>" + splitRow(line).map(function (c, i) { return cellHtml("td", c, i); }).join("") + "</tr>";
    });
    return html + "</tbody></table></div>";
  }

  function renderList(lines) {
    // Вложенные списки по отступам (для чата хватает двух уровней).
    var html = "", stack = [];
    lines.forEach(function (line) {
      var m = LIST_ITEM.exec(line);
      if (!m) {
        html += " " + inline(line.trim());
        return;
      }
      var indent = m[1].replace(/\t/g, "    ").length;
      var tag = /\d/.test(m[2]) ? "ol" : "ul";
      while (stack.length && indent < stack[stack.length - 1].indent) {
        html += "</li></" + stack.pop().tag + ">";
      }
      var top = stack[stack.length - 1];
      if (!top || indent > top.indent) {
        html += "<" + tag + ">";
        stack.push({ tag: tag, indent: indent });
      } else {
        html += "</li>";
      }
      html += "<li>" + inline(m[3]);
    });
    while (stack.length) html += "</li></" + stack.pop().tag + ">";
    return html;
  }

  function render(source) {
    var lines = escapeHtml(source || "").replace(/\r\n?/g, "\n").split("\n");
    var out = [], i = 0;

    while (i < lines.length) {
      var line = lines[i];

      if (/^\s*```/.test(line)) {                       // блок кода
        var lang = line.trim().slice(3).trim();
        var code = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
        i++;
        out.push('<pre class="md-code"' + (lang ? ' data-lang="' + lang + '"' : "") +
          "><code>" + code.join("\n") + "</code></pre>");
        continue;
      }
      if (!line.trim()) { i++; continue; }

      var heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        var level = Math.min(heading[1].length + 1, 6);  // h1 в сообщении чата слишком крупный
        out.push("<h" + level + ">" + inline(heading[2].replace(/\s+#+\s*$/, "")) + "</h" + level + ">");
        i++;
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push("<hr>"); i++; continue; }

      if (line.indexOf("|") !== -1 && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
        var rows = [line, lines[i + 1]];
        i += 2;
        while (i < lines.length && lines[i].indexOf("|") !== -1 && lines[i].trim()) rows.push(lines[i++]);
        out.push(renderTable(rows));
        continue;
      }
      if (/^\s*&gt;/.test(line)) {
        var quote = [];
        while (i < lines.length && /^\s*&gt;/.test(lines[i])) quote.push(lines[i++].replace(/^\s*&gt;\s?/, ""));
        out.push("<blockquote>" + render(unescapeForNested(quote.join("\n"))) + "</blockquote>");
        continue;
      }
      if (LIST_ITEM.test(line)) {
        var items = [];
        while (i < lines.length && lines[i].trim() &&
               (LIST_ITEM.test(lines[i]) || /^\s+\S/.test(lines[i]))) items.push(lines[i++]);
        out.push(renderList(items));
        continue;
      }
      var para = [];
      while (i < lines.length && lines[i].trim() && !/^\s*```/.test(lines[i]) &&
             !/^#{1,6}\s/.test(lines[i]) && !LIST_ITEM.test(lines[i]) && !/^\s*&gt;/.test(lines[i]) &&
             !(lines[i].indexOf("|") !== -1 && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]))) {
        para.push(inline(lines[i++]));
      }
      out.push("<p>" + para.join("<br>") + "</p>");
    }
    return out.join("\n");
  }

  // Цитату рендерим рекурсивно; экранирование снимаем, чтобы не экранировать дважды.
  function unescapeForNested(text) {
    return text.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'").replace(/&amp;/g, "&");
  }

  global.Markdown = { render: render, escapeHtml: escapeHtml };
})(window);

function render(text) { return window.Markdown.render(text); }
function escapeHtml(text) { return window.Markdown.escapeHtml(text); }
