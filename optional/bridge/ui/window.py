"""Choose exact already-open chats and their communication directions.

Controller discovery and native liveness checks are supplied by service assembly.
This UI changes only bridge connections, never native permissions or chat state.
It owns no agent-facing server and exposes no owner write through MCP.
"""
import time
import tkinter as tk
from tkinter import ttk

from optional.bridge.core.owner import OwnerControls


class Window:
    def __init__(self,root,owner, *, refresh=None,preview=False):
        if type(owner) is not OwnerControls: raise ValueError('invalid owner controller')
        self.root,self.owner,self.refresh_callback,self.preview=root,owner,refresh,preview
        self.choices={}
        root.title('Agent bridge — chat connections'+(' — preview' if preview else ''))
        root.geometry('980x620')
        root.minsize(780,480)
        frame=ttk.Frame(root,padding=20)
        frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='Choose which chats can talk to each other',font=('Segoe UI',17,'bold')).pack(anchor='w')
        ttk.Label(frame,text='Choose two chats below. Each chat keeps its current permissions.').pack(anchor='w',pady=(8,16))
        self.table=ttk.Treeview(frame,columns=('chat','runtime','workspace'),show='headings',selectmode='extended',height=9)
        for key,label,width in (('chat','Open chat',270),('runtime','App or terminal',160),('workspace','Working folder',460)):
            self.table.heading(key,text=label)
            self.table.column(key,width=width,minwidth=100)
        self.table.pack(fill='both',expand=True)
        self.table.bind('<<TreeviewSelect>>',self._selection)
        controls=ttk.Frame(frame)
        controls.pack(fill='x',pady=12)
        self.direction=tk.StringVar(value='Both directions')
        ttk.Combobox(controls,textvariable=self.direction,state='readonly',width=28,
            values=('Both directions','First selected chat → second')).pack(side='left')
        self.connect=ttk.Button(controls,text='Connect chats',command=lambda:self._change(True))
        self.connect.pack(side='left',padx=8)
        self.disconnect=ttk.Button(controls,text='Disconnect chats',command=lambda:self._change(False))
        self.disconnect.pack(side='left')
        ttk.Button(controls,text='Refresh open chats',command=self.refresh).pack(side='right')
        writers=ttk.Frame(frame)
        writers.pack(fill='x',pady=(0,12))
        # A writer lease: the first chat (a Codex app chat with an active turn) carries
        # delivery to the second for a chat of another app. It lasts until Stop, a PM revoke, a
        # service restart, or either chat coming back as a different chat binding (a permission change).
        self.lend=ttk.Button(writers,text='Let first chat deliver to second for other apps',
            command=lambda:self._lease(True))
        self.lend.pack(side='left')
        self.unlend=ttk.Button(writers,text='Stop',command=lambda:self._lease(False))
        self.unlend.pack(side='left',padx=8)
        # Stops the lease selected in the list below by its references; it works for a
        # lease whose chat is no longer listed (closed, between turns or not yet rediscovered).
        self.stop_row=ttk.Button(writers,text='Stop selected lease',command=self._stop_selected,
            state='disabled' if preview else 'normal')
        self.stop_row.pack(side='left')
        self.selected=tk.StringVar(value='Select two chats to choose a connection.')
        ttk.Label(frame,textvariable=self.selected).pack(anchor='w',pady=(0,12))
        ttk.Label(frame,text='Current connections',font=('Segoe UI',11,'bold')).pack(anchor='w')
        self.routes=tk.Listbox(frame,height=5,activestyle='none',font=('Segoe UI',10),borderwidth=1)
        self.routes.pack(fill='x',pady=6)
        self.status=tk.StringVar(value='Preview only. Connections are not changed.' if preview else 'No chat is connected just because it is listed.')
        ttk.Label(frame,textvariable=self.status,wraplength=920).pack(anchor='w',pady=(8,0))
        self._order=[]
        self._lease_rows={}
        self.refresh()

    def _selection(self,event=None):
        chosen=list(self.table.selection())
        self._order=[item for item in self._order if item in chosen]+[item for item in chosen if item not in self._order]
        if len(self._order)==2:
            titles=[self.choices[ref].title for ref in self._order]
            self.selected.set('First: '+titles[0]+'    Second: '+titles[1])
        else: self.selected.set('Select exactly two chats. The first selection is the sender for a one-way connection.')
        state='normal' if len(self._order)==2 and not self.preview else 'disabled'
        for button in (self.connect,self.disconnect,self.lend,self.unlend): button.configure(state=state)

    def refresh(self):
        try:
            if self.refresh_callback is not None: self.refresh_callback()
            choices=self.owner.chats()
            self.choices={choice.reference:choice for choice in choices}
            old=list(self._order)
            self.table.delete(*self.table.get_children())
            for choice in choices:
                self.table.insert('',tk.END,iid=choice.reference,values=(choice.title,choice.runtime,choice.workspace))
            self.table.selection_set([ref for ref in old if ref in self.choices])
            self.routes.delete(0,tk.END)
            self._lease_rows={}  # reset before any row, so a failed refresh leaves no stale index map
            for source,target in self.owner.connections():
                self.routes.insert(tk.END,self.choices[source].title+' → '+self.choices[target].title)
            for writer,target,granted,listed in self.owner.writer_grants():
                self._lease_rows[self.routes.size()]=(writer,target)
                self.routes.insert(tk.END,self._title(writer)+' delivers to '+self._title(target)
                    +' for other apps, since '+_when(granted)+('' if listed else ' (a chat is not listed now; it cannot deliver)'))
            if not self.preview and not choices: self.status.set('No available chats yet. Open a supported chat with the bridge plugin enabled.')
            self._selection()
        except Exception:
            self.status.set('Chat discovery is unavailable. Existing connections are not changed by this refresh.')
            for button in (self.connect,self.disconnect,self.lend,self.unlend): button.configure(state='disabled')

    def _title(self,reference):
        choice=self.choices.get(reference)
        return choice.title if choice is not None else 'chat '+reference[:8]+' (not listed)'

    def _stop_selected(self):
        if self.preview: return
        rows=[self._lease_rows[index] for index in self.routes.curselection() if index in self._lease_rows]
        if len(rows)!=1:
            self.status.set('Select one lease in the list to stop it.')
            return
        try:
            if self.owner.lease(*rows[0],enabled=False): self.status.set('That lease is stopped.')
            else: self.status.set('That lease was already gone or replaced. Nothing was stopped.')
        except Exception:
            self.status.set('The lease could not be stopped. Refresh and try again.')
        self.refresh()

    def _change(self,enabled):
        if self.preview or len(self._order)!=2: return
        try:
            self.owner.change(*self._order,enabled=enabled,bidirectional=self.direction.get()=='Both directions')
            self.status.set('Chats connected. Each keeps its current permissions.' if enabled else 'Chats disconnected. The chats remain open.')
        except Exception:
            self.status.set('The selected chats changed or are unavailable. Refresh the list and choose them again.')
        self.refresh()

    def _lease(self,enabled):
        if self.preview or len(self._order)!=2: return
        try:
            granted=self.owner.lease(*self._order,enabled=enabled)
            self.status.set('Since '+_when(granted)+' the first chat may deliver to the second for other apps, '
                'until you click Stop. It delivers only while it is in an active turn.'
                if enabled else 'The first chat no longer delivers to the second.')
        except Exception:
            self.status.set('The selected chats changed or are unavailable. Refresh the list and choose them again.')
        self.refresh()


def _when(granted):
    return time.strftime('%Y-%m-%d %H:%M',time.localtime(granted))
