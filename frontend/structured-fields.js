'use strict';
(() => {
  const forbiddenNames = new Set(['constructor','prototype','__proto__']);
  const scalarTypes = new Set(['string','boolean','number','integer','string_array']);
  const safeMax = Number.MAX_SAFE_INTEGER;
  const maxDefinitionBytes = 16 * 1024;
  const invalid = () => { throw new Error('字段定义无效，请检查 JSON 结构、类型和限制。'); };

  function exactObject(value, keys) {
    return value !== null && typeof value === 'object' && !Array.isArray(value)
      && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null)
      && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value,key));
  }

  function validName(name) {
    return typeof name === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(name)
      && !forbiddenNames.has(name);
  }

  function definitionTree(value) {
    if (!exactObject(value,['fields'])) invalid();
    let nodeCount = 0;
    function checkFields(fields, depth) {
      if (depth > 4 || !Array.isArray(fields) || fields.length < 1 || fields.length > 32) invalid();
      const clean = [], names = new Set(); let required = false;
      for (const field of fields) {
        if (!field || typeof field !== 'object' || Array.isArray(field)
          || !Object.hasOwn(field,'name') || !Object.hasOwn(field,'type') || !Object.hasOwn(field,'required')
          || !validName(field.name) || names.has(field.name) || typeof field.required !== 'boolean'
          || typeof field.type !== 'string') invalid();
        names.add(field.name); required ||= field.required;
        nodeCount += 1; if (nodeCount > 128) invalid();
        if (scalarTypes.has(field.type)) {
          if (!exactObject(field,['name','type','required'])) invalid();
          clean.push({name:field.name,type:field.type,required:field.required});
        } else if (field.type === 'object') {
          if (!exactObject(field,['name','type','required','fields'])) invalid();
          clean.push({name:field.name,type:'object',required:field.required,fields:checkFields(field.fields,depth+1)});
        } else if (field.type === 'array') {
          if (!exactObject(field,['name','type','required','items'])) invalid();
          clean.push({name:field.name,type:'array',required:field.required,items:checkItem(field.items,depth+1)});
        } else invalid();
      }
      if (!required) invalid();
      return clean;
    }
    function checkItem(item, depth) {
      if (depth > 4 || !item || typeof item !== 'object' || Array.isArray(item)
        || !Object.hasOwn(item,'type') || typeof item.type !== 'string') invalid();
      nodeCount += 1; if (nodeCount > 128) invalid();
      if (scalarTypes.has(item.type)) {
        if (!exactObject(item,['type'])) invalid();
        return {type:item.type};
      }
      if (item.type === 'object') {
        if (!exactObject(item,['type','fields'])) invalid();
        return {type:'object',fields:checkFields(item.fields,depth+1)};
      }
      if (item.type === 'array') {
        if (!exactObject(item,['type','items'])) invalid();
        return {type:'array',items:checkItem(item.items,depth+1)};
      }
      invalid();
    }
    const clean = {fields:checkFields(value.fields,1)};
    return clean;
  }

  function asciiJsonString(value) {
    return JSON.stringify(value).replace(/[\u007f-\uffff]/g, ch => {
      const code = ch.charCodeAt(0).toString(16).padStart(4,'0');
      return '\\u' + code;
    });
  }

  function compareKeys(a, b) {
    const left = Array.from(a, ch => ch.codePointAt(0));
    const right = Array.from(b, ch => ch.codePointAt(0));
    for (let i=0; i<Math.min(left.length,right.length); i++) {
      if (left[i] !== right[i]) return left[i]-right[i];
    }
    return left.length-right.length;
  }

  function canonicalAscii(value) {
    if (value === null || typeof value === 'boolean' || typeof value === 'number') return JSON.stringify(value);
    if (typeof value === 'string') return asciiJsonString(value);
    if (Array.isArray(value)) return '[' + value.map(canonicalAscii).join(',') + ']';
    const keys = Object.keys(value).sort(compareKeys);
    return '{' + keys.map(key => asciiJsonString(key)+':'+canonicalAscii(value[key])).join(',') + '}';
  }

  function parseStrict(text) {
    if (typeof text !== 'string') invalid();
    let index = 0;
    const whitespace = () => { while (/[\u0009\u000a\u000d\u0020]/.test(text[index] || '')) index++; };
    function stringToken() {
      const start = index++;
      while (index < text.length) {
        const code = text.charCodeAt(index);
        if (code === 34) {
          index++;
          try { return JSON.parse(text.slice(start,index)); } catch (_) { invalid(); }
        }
        if (code < 32) invalid();
        if (code === 92) {
          index++;
          if (index >= text.length) invalid();
          const escaped = text[index++];
          if (escaped === 'u') {
            if (!/^[0-9a-fA-F]{4}$/.test(text.slice(index,index+4))) invalid();
            index += 4;
          } else if (!'"\\/bfnrt'.includes(escaped)) invalid();
        } else index++;
      }
      invalid();
    }
    function value() {
      whitespace();
      const ch = text[index];
      if (ch === '"') return stringToken();
      if (ch === '{') {
        index++; whitespace();
        const result = Object.create(null), seen = new Set();
        if (text[index] === '}') { index++; return result; }
        while (index < text.length) {
          whitespace(); if (text[index] !== '"') invalid();
          const key = stringToken(); if (seen.has(key)) invalid(); seen.add(key);
          whitespace(); if (text[index++] !== ':') invalid();
          result[key] = value(); whitespace();
          if (text[index] === '}') { index++; return result; }
          if (text[index++] !== ',') invalid();
        }
        invalid();
      }
      if (ch === '[') {
        index++; whitespace(); const result = [];
        if (text[index] === ']') { index++; return result; }
        while (index < text.length) {
          result.push(value()); whitespace();
          if (text[index] === ']') { index++; return result; }
          if (text[index++] !== ',') invalid();
        }
        invalid();
      }
      for (const [literal, parsed] of [['true',true],['false',false],['null',null]]) {
        if (text.startsWith(literal,index)) { index += literal.length; return parsed; }
      }
      const match = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(text.slice(index));
      if (!match) invalid();
      index += match[0].length;
      const number = Number(match[0]);
      if (!Number.isFinite(number) || Math.abs(number) > safeMax) invalid();
      return number;
    }
    const parsed = value(); whitespace();
    if (index !== text.length) invalid();
    return parsed;
  }

  function validDefinition(value) {
    try {
      const clean = definitionTree(value);
      return canonicalAscii(clean).length <= maxDefinitionBytes;
    } catch (_) { return false; }
  }

  function parseDefinition(text) {
    try {
      if (typeof text !== 'string' || new TextEncoder().encode(text).length > maxDefinitionBytes) invalid();
      const parsed = parseStrict(text);
      const clean = definitionTree(parsed);
      if (canonicalAscii(clean).length > maxDefinitionBytes) invalid();
      return clean;
    } catch (_) { invalid(); }
  }

  function scalarFields(definition) {
    const clean = definitionTree(definition);
    const output = [];
    function visit(fields, prefix) {
      for (const field of fields) {
        if (!field.required) continue;
        const path = [...prefix,field.name];
        if (['string','boolean','number','integer'].includes(field.type)) output.push({path,type:field.type});
        else if (field.type === 'object') visit(field.fields,path);
      }
    }
    visit(clean.fields,[]);
    return output;
  }

  globalThis.AgentLineStructuredFields = {validDefinition,parseDefinition,scalarFields};
})();
