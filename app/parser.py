"""Strict CSV/XLSX parser for migration input; no formula evaluation, no macros."""
import csv,io,re,zipfile,xml.etree.ElementTree as ET
from fastapi import HTTPException
MAX_BYTES=8*1024*1024
MAX_ROWS=10000
MAX_COLS=120
MAX_CELL=10000

def clean(value):
 s=str(value or '').strip()
 if len(s)>MAX_CELL:
  raise HTTPException(422,'Una celda supera el límite de 10000 caracteres; no se ha importado ni recortado el contenido')
 return s
def read_rows(data:bytes,filename:str):
 if not data or len(data)>MAX_BYTES:raise HTTPException(413,'Archivo vacío o mayor de 8 MB')
 suffix=filename.lower().rsplit('.',1)[-1]
 if suffix=='csv':
  try:content=data.decode('utf-8-sig')
  except UnicodeDecodeError:
   try:content=data.decode('cp1252')
   except UnicodeDecodeError:raise HTTPException(422,'Codificación no compatible')
  delim= ';' if content.splitlines()[0].count(';')>content.splitlines()[0].count(',') else ','
  reader=csv.reader(io.StringIO(content),delimiter=delim)
  rows=[]
  for i,r in enumerate(reader):
   if i>MAX_ROWS:raise HTTPException(413,'Demasiadas filas; dividir archivo')
   rows.append([clean(v) for v in r])
 elif suffix=='xlsx':rows=read_xlsx(data)
 else:raise HTTPException(415,'Solo CSV y XLSX')
 if not rows:raise HTTPException(422,'Archivo sin filas')
 headers=rows[0]
 if len(headers)>MAX_COLS or not any(headers):raise HTTPException(422,'Cabeceras inválidas')
 if len(set(h.lower() for h in headers))!=len(headers):raise HTTPException(422,'Columnas repetidas; renombrar cabeceras')
 if any(not h for h in headers):raise HTTPException(422,'Hay cabeceras vacías')
 output=[]
 for i,row in enumerate(rows[1:],start=2):
  if not any(row):continue
  if len(row)>len(headers):raise HTTPException(422,f'Fila {i}: columnas adicionales')
  output.append({h:row[j] if j<len(row) else '' for j,h in enumerate(headers)})
 return headers,output

def read_xlsx(data):
 ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main','r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
 try:
  with zipfile.ZipFile(io.BytesIO(data)) as z:
   names=set(z.namelist())
   if len(names)>200 or sum(i.file_size for i in z.infolist())>45*1024*1024:raise HTTPException(413,'XLSX excede límites de descompresión')
   if any('vbaProject' in n for n in names):raise HTTPException(415,'Macros no admitidas')
   shared=[]
   if 'xl/sharedStrings.xml' in names:
    root=ET.fromstring(z.read('xl/sharedStrings.xml'))
    shared=[''.join(t.text or '' for t in si.findall('.//m:t',ns)) for si in root.findall('m:si',ns)]
   sheets=sorted(n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',n))
   if not sheets:raise HTTPException(422,'XLSX sin hoja de trabajo')
   root=ET.fromstring(z.read(sheets[0]));rows=[]
   for ix,row in enumerate(root.findall('.//m:sheetData/m:row',ns)):
    if ix>MAX_ROWS:raise HTTPException(413,'Demasiadas filas')
    cells={}
    for c in row.findall('m:c',ns):
     ref=c.get('r','A1');letters=re.match('[A-Z]+',ref)
     if not letters:continue
     idx=0
     for ch in letters.group():idx=idx*26+(ord(ch)-64)
     idx-=1
     if idx>=MAX_COLS:raise HTTPException(413,'Demasiadas columnas')
     v=c.find('m:v',ns)
     value=v.text if v is not None else ''
     if c.get('t')=='s' and value:value=shared[int(value)]
     elif c.get('t')=='inlineStr':value=''.join(x.text or '' for x in c.findall('.//m:t',ns))
     cells[idx]=clean(value)
    rows.append([cells.get(i,'') for i in range(max(cells,default=-1)+1)])
   return rows
 except (zipfile.BadZipFile,ET.ParseError,IndexError,ValueError,KeyError) as e:raise HTTPException(422,'XLSX dañado o incompatible') from e
