import fs from 'node:fs/promises';
import {FileBlob, SpreadsheetFile} from '@oai/artifact-tool';

const source = process.argv[2];
if (!source) throw new Error('Usage: node scripts/prepare_sheet.mjs /absolute/source.xlsx');
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(source));
const sheet = workbook.worksheets.getItem('Лист1');
const sourceRows = sheet.getRange('A1:BL74').values;
const ids = [];
let section = 'ОВ 3';
let serial = 0;
for (let row = 5; row <= 71; row++) {
  if (row === 53) { section = 'ЭМ3'; serial = 0; }
  const cells = sourceRows[row - 1];
  if (!cells[1] || !cells[2] || typeof cells[3] !== 'number') continue;
  const id = `${section === 'ОВ 3' ? 'OV3' : 'EM3'}-${String(++serial).padStart(3, '0')}`;
  ids.push({id, row, section, name: cells[1], unit: String(cells[2]).toLowerCase()});
}

sheet.getRange('A1:BL74').clear({applyTo:'formats'});
sheet.getRange('A1:I2').unmerge();
for (const col of ['A','B','C','D','E','F','G','H','I']) sheet.getRange(`${col}1:${col}2`).merge();
const headers = ['ID позиции', 'Наименование по спецификации', 'Ед. изм.', 'Объём по смете',
  'ЗП за ед.', 'ЗП по смете', 'Остаток объёма', 'Выполнено в деньгах', 'Выполненный объём'];
headers.forEach((title,i)=>{sheet.getCell(0,i).values=[[title]];});
sheet.getRange('A3:I3').clear({applyTo:'contents'});
// The duplicated 30 September column contains no daily quantities in the source.
// Reuse that empty column for the initial current day without changing history.
sheet.getRange('AW3').values = [[46328]]; // Excel/Sheets serial for 02.11.2026.
sheet.getRange('AX3:BL4').clear({applyTo:'contents'});
for (const {id,row,unit} of ids) {
  sheet.getRange(`A${row}`).values=[[id]];
  sheet.getRange(`C${row}`).values=[[unit]];
  sheet.getRange(`F${row}`).formulas=[[`=D${row}*E${row}`]];
  sheet.getRange(`G${row}`).formulas=[[`=D${row}-I${row}`]];
  sheet.getRange(`H${row}`).formulas=[[`=I${row}*E${row}`]];
  sheet.getRange(`I${row}`).formulas=[[`=SUM(J${row}:ZZ${row})`]];
}
for (const row of [16,32,42,52,53]) sheet.getRange(`A${row}`).clear({applyTo:'contents'});
sheet.getRange('F52').formulas=[['=SUM(F5:F51)']];
sheet.getRange('F72').formulas=[['=SUM(F54:F71)']];
sheet.getRange('I52').clear({applyTo:'contents'});
sheet.getRange('F74').values=[['Всего по объекту']];
sheet.getRange('H74').formulas=[['=H52+H72']];
sheet.getRange('J4:AW4').values=[Array(40).fill('объём')];
sheet.getRange('A1:AW74').format={font:{name:'Arial',size:10,color:'#202124'},
  verticalAlignment:'center',rowHeight:44};
sheet.getRange('B5:B71').format.wrapText=true;
sheet.getRange('A1:I2').format={fill:'#E8EAED',font:{bold:true},wrapText:true,horizontalAlignment:'center'};
sheet.getRange('A1:I2').format.rowHeight=28;
sheet.getRange('A:A').format.columnWidthPx=105;
sheet.getRange('B:B').format.columnWidthPx=540;
sheet.getRange('C:C').format.columnWidthPx=62;
sheet.getRange('D:I').format.columnWidthPx=115;
sheet.getRange('J:AW').format.columnWidthPx=105;
sheet.getRange('J3:AW4').format={fill:'#F1F3F4',horizontalAlignment:'center',font:{bold:true},rowHeight:24};
for (const row of [4,16,32,42,53]) {
  sheet.getRange(`A${row}:AW${row}`).format={fill:'#F1F3F4',font:{bold:true},rowHeight:28};
}
for (const row of [52,72,74]) {
  sheet.getRange(`A${row}:I${row}`).format={fill:'#E8EAED',font:{bold:true},rowHeight:32};
}
sheet.getRange('G5:I71').format.fill='#F8F9FA';
sheet.getRange('G5:G71').conditionalFormats.add('cellIs',{
  operator:'lessThan',formula:0,format:{fill:'#FCE8E6',font:{color:'#B3261E'}}});
sheet.getRange('AW3:AW4').format.fill='#E6F4EA';
sheet.getRange('D5:I74').setNumberFormat('#,##0.00');
sheet.getRange('J5:AW74').setNumberFormat('0.00');
sheet.getRange('J3:AW3').setNumberFormat('dd.mm.yyyy');
// Keep the referenced total range inside the exported/imported grid.
sheet.getRange('ZZ1').format.fill='#FFFFFF';
sheet.freezePanes.freezeRows(4);
sheet.freezePanes.freezeColumns(2);
sheet.showGridLines=true;
workbook.recalculate();
await fs.mkdir('artifacts',{recursive:true});
await fs.writeFile('artifacts/catalog-reference.json',JSON.stringify(ids,null,2));
const check=await workbook.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!',
  options:{useRegex:true,maxResults:10},summary:'formula validation'});
console.log(check.ndjson);
const render=await workbook.render({sheetName:'Лист1',range:'A1:J10',scale:1,format:'png'});
await fs.writeFile('artifacts/prepared-preview.png',new Uint8Array(await render.arrayBuffer()));
const out=await SpreadsheetFile.exportXlsx(workbook);
await out.save('artifacts/m100-ready.xlsx');
console.log(`Prepared ${ids.length} specification positions; output artifacts/m100-ready.xlsx`);
