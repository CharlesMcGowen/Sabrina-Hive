"""Read-only, lazy desktop catalog and screenshot viewer."""
import base64
import json
import threading
import time
import tkinter as tk
from tkinter import ttk

from tools.desktop_trace_reader import DesktopTraceReader
from tools.agent_desktop_registry import REGISTRY


class DesktopCatalogView(ttk.Frame):
    def __init__(self,parent,reader=None):
        super().__init__(parent)
        self.reader=reader or DesktopTraceReader();self.epoch=0;self.expires=0;self.image=None;self.page=0
        bar=ttk.Frame(self);bar.pack(fill='x')
        self.run=ttk.Combobox(bar,state='readonly',width=45);self.run.pack(side='left');self.run.bind('<<ComboboxSelected>>',lambda _:self.load())
        ttk.Button(bar,text='Refresh runs',command=self.refresh).pack(side='left')
        ttk.Button(bar,text='Previous',command=lambda:self.paginate(-1)).pack(side='left')
        ttk.Button(bar,text='Next',command=lambda:self.paginate(1)).pack(side='left')
        self.summary=tk.Text(self,height=13,wrap='word',state='disabled');self.summary.pack(fill='x')
        self.artifacts=ttk.Treeview(self,columns=('kind','app','hash'),show='headings',height=5)
        for column in ('kind','app','hash'): self.artifacts.heading(column,text=column)
        self.artifacts.pack(fill='x');self.artifacts.bind('<<TreeviewSelect>>',lambda _:self.show_image())
        self.preview=ttk.Label(self,text='Select a retained screenshot to view it.');self.preview.pack(fill='both',expand=True)
        self.after(1000,self.expiry_tick)

    def background(self, work, apply):
        token=self.epoch
        def run():
            try: value=work()
            except Exception as exc: value={'error':str(exc)}
            try: self.after(0,lambda:apply(value) if token==self.epoch and self.winfo_exists() else None)
            except RuntimeError: pass
        threading.Thread(target=run,daemon=True).start()

    def refresh(self):
        self.background(lambda:self.reader.list_runs(limit=50),self.set_runs)

    def set_runs(self,rows):
        if not isinstance(rows,list): return
        self.run['values']=[r['run_id'] for r in rows]

    def paginate(self,delta):
        self.page=max(0,self.page+delta);self.load(reset=False)

    def load(self,reset=True):
        self.epoch+=1;self.clear_image()
        if reset: self.page=0
        rid=self.run.get();page=self.page
        if rid: self.background(lambda:self.reader.desktop_capability_state(rid,page*100),self.render)

    def render(self,state):
        self.expires=state.get('expires_at',0)
        text=json.dumps({k:v for k,v in state.items() if k!='artifacts'},indent=2)
        permissions=state.get('permissions',{})
        text+='\n\nCommand coverage (installed catalog):\n'+'\n'.join(
            f"{n}: {e['state']} | {e['permission'] or 'app scope'} | permission={permissions.get(e['permission'],False) if e['permission'] else 'n/a'}"
            for n,e in REGISTRY.items())
        self.summary.configure(state='normal');self.summary.delete('1.0','end');self.summary.insert('end',text);self.summary.configure(state='disabled')
        self.artifacts.delete(*self.artifacts.get_children())
        for a in state.get('artifacts',[]): self.artifacts.insert('','end',iid=a['id'],values=(a['kind'],a['app'],a['sha256'][:16]))
        if state.get('is_expired'): self.clear_image('Raw evidence expired; metadata only.')

    def show_image(self):
        selection=self.artifacts.selection()
        if not selection: return
        rid=self.run.get();identity=selection[0]
        self.background(lambda:self.reader.desktop_image(rid,identity),self.render_image)

    def render_image(self,content):
        if not isinstance(content,bytes) or self.expires<=time.time(): self.clear_image('Image unavailable or expired.');return
        try:
            image=tk.PhotoImage(data=base64.b64encode(content).decode())
            factor=max(1,(image.width()+999)//1000,(image.height()+599)//600)
            self.image=image.subsample(factor);self.preview.configure(image=self.image,text='')
        except tk.TclError: self.clear_image('Invalid PNG artifact.')

    def clear_image(self,text='Select a retained screenshot to view it.'):
        self.image=None;self.preview.configure(image='',text=text)

    def expiry_tick(self):
        if self.expires and self.expires<=time.time():
            self.epoch+=1;self.clear_image('Raw evidence expired; metadata only.')
        if self.winfo_exists(): self.after(1000,self.expiry_tick)
