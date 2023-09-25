from __future__ import print_function
import logging
import os.path
import sys
import argparse

import random
import fnmatch
from functools import partial
import subprocess
import threading
import sys
import platform

from Qt import QtCore, QtWidgets, QtGui
from pxr import Usd, Sdf, Ar, UsdUtils, Tf

from . import utils, text_view, info_panel
from .vendor.Nodz import nodz_main

import re
from pprint import pprint


digitSearch = re.compile(r'\b\d+\b')
logger = logging.getLogger('usd-noodle')
logger.setLevel(logging.INFO)
if not len(logger.handlers):
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    # fh = logging.FileHandler('/tmp/usd-noodle/test.log')
    # fh.setLevel(logging.DEBUG)
    logger.addHandler(ch)
    # logger.addHandler(fh)
logger.propagate = False


def launch_usdview(usdfile):
    print('launching usdview', usdfile)
    subprocess.call(['usdview', usdfile], shell=True)


class DependencyWalker(object):
    def __init__(self, usdfile):
        self.usdfile = usdfile
        self.walk_attributes = True
        
        logger.info('DependencyWalker'.center(40, '-'))
        logger.info('Loading usd file: {}'.format(self.usdfile))
        self.nodes = {}
        self.edges = []
        
        self.resolver = Ar.GetResolver()
        
        self.visited_nodes = []
        
        self.errored_nodes = []
    
    
    def start(self):
        self.visited_nodes = []
        self.nodes = {}
        self.edges = []
        self.init_edges = []
        
        layer = Sdf.Layer.FindOrOpen(self.usdfile)
        if not layer:
            return
        
        # scrub the initial file path
        # to get around upper/lowercase drive letters
        # and junk like that
        layer_path = Sdf.ComputeAssetPathRelativeToLayer(layer, os.path.basename(self.usdfile))
        
        self.usdfile = layer_path
        
        info = {}
        info['online'] = os.path.isfile(layer_path)
        info['path'] = layer_path
        info['type'] = 'sublayer'
        self.nodes[layer_path] = info
        
        self.walkStageLayers(layer_path)
        
        for edge in self.edges:
            start = edge[0]
            end = edge[1]
            
            info = self.nodes[end]
            info['count'] = info.get("count", 0) + 1
            self.nodes[end] = info
    
    
    def get_flat_child_list(self, path):
        ret = [path]
        for key, child in path.nameChildren.items():
            ret.extend(self.get_flat_child_list(child))
        ret = list(set(ret))
        return ret
    
    
    def flatten_ref_list(self, ref_or_payload):
        ret = []
        for itemlist in [ref_or_payload.appendedItems, ref_or_payload.explicitItems, ref_or_payload.addedItems,
                         ref_or_payload.prependedItems, ref_or_payload.orderedItems]:
            for payload in itemlist:
                ret.append(payload)
        return list(set(ret))
    
    
    def resolve(self, layer, path):
        stage = Usd.Stage.Open(layer)
        if stage:
            resolved_path = stage.ResolveIdentifierToEditTarget(path)
            return resolved_path
        resolved = self.resolver.Resolve(path)
        if resolved:
            return resolved.GetPathString()
        else:
            # resolver will return None on invalid paths
            # we still want the path regardless
            return path
    
    
    def walkStageLayers(self, layer_path, level=1):
        id = '-' * (level)
        
        sublayers = []
        payloads = []
        references = []
        
        try:
            layer = Sdf.Layer.FindOrOpen(layer_path)
        except Tf.ErrorException as e:
            info = {}
            info['online'] = True
            info['error'] = True
            info['path'] = layer_path
            self.nodes[layer_path] = info
            self.errored_nodes.append(layer_path)
            logger.info('usd file: {} had load errors'.format(layer_path))
            return
        
        if not layer:
            return
        # print(id, layer.realPath)
        root = layer.pseudoRoot
        # print(id, 'root', root)
        
        # print(id, 'children'.center(40, '-'))
        
        child_list = []
        # info packet from the root prim
        if layer_path in self.nodes:
            info = self.nodes[layer_path]
            child_list = self.get_flat_child_list(root)
            info_dict = dict()
            for key in root.ListInfoKeys():
                if key in ['subLayers', 'subLayerOffsets']:
                    continue
                info_dict[key] = root.GetInfo(key)
            
            info['info'] = info_dict
            info['specifier'] = root.specifier.displayName
            info['muted'] = layer.IsMuted()
            info['defaultPrim'] = layer.defaultPrim
            info['PseudoRoot'] = layer.pseudoRoot.name
            info['RootPrims'] = [x.path.GetPrimPath().pathString for x in layer.rootPrims]
            self.nodes[layer_path] = info
        
        for child in child_list:
            # print(id, child)
            
            if self.walk_attributes:
                attributes = child.attributes
                for attr in attributes:
                    # we are looking for "asset" type attributes
                    # references to external things
                    if attr.typeName == 'asset':
                        value = attr.default
                        # sometimes you get empty paths
                        if not value:
                            continue
                        if not value.path:
                            continue
                        
                        resolved_path = self.resolve(layer, value.path)
                        info = {}
                        info['online'] = os.path.isfile(resolved_path)
                        info['path'] = resolved_path
                        filebase, ext = os.path.splitext(resolved_path)
                        info['type'] = 'ext'
                        if ext in ['.jpg', '.tex', '.tx', '.png', '.exr', '.hdr', '.tga', '.tif', '.tiff',
                                   '.pic', '.gif', '.psd', '.ptex', '.cin', '.dpx', '.bmp', '.iff',
                                   '.mov', '.m4v', '.mp4', '.webp']:
                            info['type'] = 'tex'
                            info['colorspace'] = attr.colorSpace
                        
                        self.nodes[resolved_path] = info
                        
                        # so, we want to find out if this attribute is inside a shader
                        # it's conceivable that asset attrs could exist outside of shaders
                        # i just havent seen that in the wild yet
                        # crawl through the ancestors - ie Material -> Shader -> Attribute
                        owner = attr.owner
                        owner_type = owner.typeName
                        if owner_type == 'Shader':
                            owner_parent = owner.nameParent
                            if owner_parent.typeName == 'Material':
                                material_path = '{}:{}'.format(os.path.splitext(layer.realPath)[0], owner_parent.name)
                                info = {}
                                info['online'] = True
                                info['path'] = material_path
                                info['type'] = 'material'
                                
                                self.nodes[material_path] = info
                                
                                # connect the material to the layer
                                if not [layer_path, material_path, 'materials'] in self.edges:
                                    self.edges.append([layer_path, material_path, 'materials'])
                                
                                # then connect the file to the material
                                if not [material_path, resolved_path, owner.name] in self.edges:
                                    self.edges.append([material_path, resolved_path, owner.name])
                                
                                continue
                        
                        # finally, if it doesn't smell like a material
                        # then just set up a regular connectio to the layer
                        if not [layer_path, resolved_path, info['type']] in self.edges:
                            self.edges.append([layer_path, resolved_path, info['type']])
            
            clip_info = child.GetInfo("clips")
            # pprint(clip_info)
            for clip_set_name in clip_info:
                clip_set = clip_info[clip_set_name]
                # print(clip_set_name, clip_set.get("assetPaths"), clip_set.get("manifestAssetPath"), clip_set.get()
                #     "primPath")
                
                """
                @todo: subframe handling
                integer frames: path/basename.###.usd
                subinteger frames: path/basename.##.##.usd.
                
                @todo: non-1 increments
                """
                clip_asset_paths = clip_set.get("assetPaths")
                # don't use resolved path in case either the first or last file is missing from disk
                firstFile = str(clip_asset_paths[0].path)
                lastFile = str(clip_asset_paths[-1].path)
                if digitSearch.findall(firstFile):
                    firstFileNum = digitSearch.findall(firstFile)[-1]
                else:
                    firstFileNum = '???'
                
                if digitSearch.findall(lastFile):
                    lastFileNum = digitSearch.findall(lastFile)[-1]
                else:
                    lastFileNum = '???'
                digitRange = str(firstFileNum + '-' + lastFileNum)
                nodeName = ''
                
                firstFileParts = firstFile.split(firstFileNum)
                for i in range(len(firstFileParts) - 1):
                    nodeName += str(firstFileParts[i])
                
                nodeName += digitRange
                nodeName += firstFileParts[-1]
                
                allFilesFound = True
                for path in clip_asset_paths:
                    clip_path = self.resolve(layer, path.path)
                    if not os.path.isfile(clip_path):
                        allFilesFound = False
                        break
                
                # TODO : make more efficient - looping over everything currently
                # TODO: validate presence of all files in the clip seq. bg thread?
                
                manifestPath = clip_set.get("manifestAssetPath")
                refpath = self.resolve(layer, clip_asset_paths[0].path)
                clipmanifest_path = self.resolve(layer, manifestPath.path)
                
                info = {}
                info['online'] = allFilesFound
                info['path'] = refpath
                info['type'] = 'clip'
                info['primPath'] = clip_set.get("primPath")
                info['clipSet'] = clip_set_name
                
                self.nodes[nodeName] = info
                
                if not [layer_path, nodeName, 'clip'] in self.edges:
                    self.edges.append([layer_path, nodeName, 'clip'])
                
                if not [nodeName, clipmanifest_path, 'manifest'] in self.edges:
                    self.edges.append([nodeName, clipmanifest_path, 'manifest'])
            
            if child.variantSets:
                for varset in child.variantSets:
                    # print(child, 'variant set', varset.name)
                    variant_path = '{}:{}'.format(os.path.splitext(layer.realPath)[0], varset.name)
                    varprim = varset.owner
                    
                    info = {}
                    info['online'] = True
                    info['path'] = variant_path
                    info['type'] = 'variant'
                    info['variant_set'] = varset.name
                    info['variants'] = [str(x) for x in varset.variants.keys()]
                    
                    info['current_variant'] = varprim.variantSelections.get(varset.name)
                    
                    self.nodes[variant_path] = info
                    
                    if not [layer_path, variant_path, 'variant'] in self.edges:
                        self.edges.append([layer_path, variant_path, 'variant'])
                    
                    for variant_name in varset.variants.keys():
                        variant = varset.variants[variant_name]
                        
                        # so variants can host payloads and references
                        # we get to these through the variants primspec
                        # and then add them to our list of paths to inspect
                        if variant_name != info.get('current_variant'):
                            continue
                        for primspec_child in self.get_flat_child_list(variant.primSpec):
                            
                            for payload in self.flatten_ref_list(primspec_child.payloadList):
                                pathToResolve = payload.assetPath
                                if pathToResolve:
                                    refpath = self.resolve(layer, pathToResolve)
                                    payloads.append(refpath)
                                    
                                    info = {}
                                    info['online'] = os.path.isfile(refpath)
                                    info['path'] = refpath
                                    info['type'] = 'payload'
                                    
                                    self.nodes[refpath] = info
                                    
                                    if not [variant_path, refpath, variant_name] in self.edges:
                                        self.edges.append([variant_path, refpath, variant_name])
                            
                            for reference in self.flatten_ref_list(primspec_child.referenceList):
                                pathToResolve = reference.assetPath
                                if pathToResolve:
                                    refpath = self.resolve(layer, pathToResolve)
                                    references.append(refpath)
                                    
                                    info = {}
                                    info['online'] = os.path.isfile(refpath)
                                    info['path'] = refpath
                                    info['type'] = 'reference'
                                    
                                    self.nodes[refpath] = info
                                    
                                    if not [variant_path, refpath, variant_name] in self.edges:
                                        self.edges.append([variant_path, refpath, variant_name])
            
            payloadList = self.flatten_ref_list(child.payloadList)
            for payload in payloadList:
                pathToResolve = payload.assetPath
                if pathToResolve:
                    refpath = self.resolve(layer, pathToResolve)
                    payloads.append(refpath)
                    
                    info = {}
                    info['online'] = os.path.isfile(refpath)
                    info['path'] = refpath
                    info['type'] = 'payload'
                    
                    self.nodes[refpath] = info
                    
                    if not [layer_path, refpath, 'payload'] in self.edges:
                        self.edges.append([layer_path, refpath, 'payload'])
            
            referenceList = self.flatten_ref_list(child.referenceList)
            for reference in referenceList:
                pathToResolve = reference.assetPath
                if pathToResolve:
                    refpath = self.resolve(layer, pathToResolve)
                    references.append(refpath)
                    
                    info = {}
                    info['online'] = os.path.isfile(refpath)
                    info['path'] = refpath
                    info['type'] = 'reference'
                    
                    self.nodes[refpath] = info
                    
                    if not [layer_path, refpath, 'reference'] in self.edges:
                        self.edges.append([layer_path, refpath, 'reference'])
        
        for rel_sublayer in layer.subLayerPaths:
            refpath = self.resolve(layer, rel_sublayer)
            sublayers.append(refpath)
            
            info = {}
            info['online'] = os.path.isfile(refpath)
            info['path'] = refpath
            info['type'] = 'sublayer'
            self.nodes[refpath] = info
            
            if not [layer_path, refpath, 'sublayer'] in self.edges:
                self.edges.append([layer_path, refpath, 'sublayer'])
        
        sublayers = list(set(sublayers))
        references = list(set(references))
        payloads = list(set(payloads))
        
        if sublayers:
            logger.debug((id, 'sublayerPaths'.center(40, '-')))
            logger.debug((id, sublayers))
        for sublayer in sublayers:
            self.walkStageLayers(sublayer, level=level + 1)
        
        if references:
            logger.debug((id, 'references'.center(40, '-')))
            logger.debug((id, references))
        for reference in references:
            self.walkStageLayers(reference, level=level + 1)
        
        if payloads:
            logger.debug((id, 'payloads'.center(40, '-')))
            logger.debug((id, payloads))
        for payload in payloads:
            self.walkStageLayers(payload, level=level + 1)


def find_node(node_coll, attr_name, attr_value):
    for x in node_coll:
        node = node_coll[x]
        if getattr(node, attr_name) == attr_value:
            return node


class FindNodeWindow(QtWidgets.QDialog):
    def __init__(self, nodz, parent=None):
        self.nodz = nodz
        super(FindNodeWindow, self).__init__(parent)
        self.setWindowFlags(QtCore.Qt.Tool | QtCore.Qt.WindowStaysOnTopHint)
        self.setWindowTitle('Find nodes')
        self.build_ui()
    
    
    def search(self):
        search_text = self.searchTxt.text()
        
        self.foundNodeList.clear()
        if search_text == '':
            return
        
        for x in sorted(self.nodz.scene().nodes):
            this_node = self.nodz.scene().nodes[x]
            if fnmatch.fnmatch(this_node.label.lower(), '*%s*' % search_text.lower()):
                self.foundNodeList.addItem(QtWidgets.QListWidgetItem(this_node.label))
    
    
    def item_selected(self, *args):
        items = self.foundNodeList.selectedItems()
        if items:
            sel = [x.text() for x in items]
            
            for x in self.nodz.scene().nodes:
                node = self.nodz.scene().nodes[x]
                if node.label in sel:
                    node.setSelected(True)
                else:
                    node.setSelected(False)
            self.nodz._focus()
    
    
    def build_ui(self):
        lay = QtWidgets.QVBoxLayout()
        self.setLayout(lay)
        self.searchTxt = QtWidgets.QLineEdit()
        self.searchTxt.textChanged.connect(self.search)
        lay.addWidget(self.searchTxt)
        
        self.foundNodeList = QtWidgets.QListWidget()
        self.foundNodeList.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.foundNodeList.itemSelectionChanged.connect(self.item_selected)
        lay.addWidget(self.foundNodeList)


class NodeGraphWindow(QtWidgets.QDialog):
    def __init__(self, usdfile=None, walk_attributes=False, parent=None):
        super(NodeGraphWindow, self).__init__(parent)
        self.settings = QtCore.QSettings("chrisg", "usd-noodle")
        self.setWindowTitle("Noodle")
        self.build_ui()
        
        self.noodle = NoodleWidget(usdfile=None, walk_attributes=walk_attributes, parent=self)
        self.noodle.file_loaded.connect(self.file_loaded)
        self.top_layout.addWidget(self.noodle)
        self.show()
        self.noodle.usdfile = usdfile
        self.noodle.load_file()
    
    
    def build_ui(self):
        if self.settings.value("geometry"):
            self.restoreGeometry(self.settings.value("geometry"))
        else:
            sizeObject = QtWidgets.QDesktopWidget().screenGeometry(-1)
            self.resize(sizeObject.width() * 0.8, sizeObject.height() * 0.8)
        
        self.setWindowFlags(
            self.windowFlags() | QtCore.Qt.WindowMinimizeButtonHint | QtCore.Qt.WindowMaximizeButtonHint)
        
        self.top_layout = QtWidgets.QVBoxLayout()
        self.top_layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self.top_layout)
    
    
    def file_loaded(self, filename):
        self.setWindowTitle("Noodle - {}".format(filename))
    
    
    def closeEvent(self, event):
        """
        Window close event. Saves preferences. Impregnates your dog.
        """
        self.noodle.cleanup()
        self.settings.setValue("geometry", self.saveGeometry())
        super(NodeGraphWindow, self).closeEvent(event)


class NoodleWidget(QtWidgets.QWidget):
    file_loaded = QtCore.Signal(object)  # string
    
    
    def __init__(self, usdfile=None, walk_attributes=False, parent=None):
        super(NoodleWidget, self).__init__(parent)
        self.settings = QtCore.QSettings("chrisg", "usd-noodle")
        
        self.usdfile = usdfile
        self.root_node = None
        
        self.nodz = None
        self.walk_attributes = walk_attributes
        
        self.find_win = None
        self.build_ui()
        
        if self.usdfile:
            self.load_file()
    
    
    def cleanup(self):
        if self.find_win:
            self.find_win.close()
        self.settings.setValue("splitterSizes", self.splitter.saveState())
    
    
    def loadTextChkChanged(self, state):
        self.walk_attributes = self.loadTextChk.isChecked()
    
    
    def build_ui(self):
        
        self.top_layout = QtWidgets.QVBoxLayout()
        # self.top_layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self.top_layout)
        
        self.toolbar_lay = QtWidgets.QHBoxLayout()
        self.top_layout.addLayout(self.toolbar_lay)
        
        noodle_label = QtWidgets.QLabel()
        icon = QtGui.QPixmap()
        icon.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons", 'noodle.png'))
        noodle_label.setPixmap(icon.scaled(32, 32,
                                           QtCore.Qt.KeepAspectRatio,
                                           QtCore.Qt.SmoothTransformation)
                               )
        
        self.toolbar_lay.addWidget(noodle_label)
        
        self.openBtn = QtWidgets.QPushButton("Open...", )
        self.openBtn.setShortcut('Ctrl+o')
        self.openBtn.clicked.connect(self.manualOpen)
        self.toolbar_lay.addWidget(self.openBtn)
        
        self.reloadBtn = QtWidgets.QPushButton("Reload")
        self.reloadBtn.setShortcut('Ctrl+r')
        self.reloadBtn.clicked.connect(self.load_file)
        self.toolbar_lay.addWidget(self.reloadBtn)
        
        self.loadTextChk = QtWidgets.QCheckBox("Load Textures")
        self.loadTextChk.setChecked(self.walk_attributes)
        self.loadTextChk.stateChanged.connect(self.loadTextChkChanged)
        self.toolbar_lay.addWidget(self.loadTextChk)
        
        self.findBtn = QtWidgets.QPushButton("Find...")
        self.findBtn.setShortcut('Ctrl+f')
        self.findBtn.clicked.connect(self.findWindow)
        self.toolbar_lay.addWidget(self.findBtn)
        
        self.layoutBtn = QtWidgets.QPushButton("Layout Nodes")
        self.layoutBtn.clicked.connect(self.layout_nodes)
        self.toolbar_lay.addWidget(self.layoutBtn)
        
        self.saveImgBtn = QtWidgets.QPushButton("Save Image")
        self.saveImgBtn.clicked.connect(self.save_image)
        self.toolbar_lay.addWidget(self.saveImgBtn)
        
        toolbarspacer = QtWidgets.QSpacerItem(10, 10, QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)
        self.toolbar_lay.addItem(toolbarspacer)
        
        self.splitter = QtWidgets.QSplitter()
        
        self.top_layout.addWidget(self.splitter)
        
        main_widget = QtWidgets.QWidget()
        main_widget.setContentsMargins(0, 0, 0, 0)
        
        self.splitter.addWidget(main_widget)
        lay = QtWidgets.QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        
        main_widget.setLayout(lay)
        
        self.top_layout.addLayout(lay)
        
        logger.info('building nodes')
        configPath = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'nodz_config.json')
        
        self.nodz = nodz_main.Nodz(self, configPath=configPath)
        self.nodz.editLevel = 1
        # self.nodz.editEnabled = False
        lay.addWidget(self.nodz)
        self.nodz.initialize()
        self.nodz.fitInView(-500, -500, 500, 500)
        self.nodz.create_overview_widget()
        
        info_scroll = QtWidgets.QScrollArea()
        info_scroll.setWidgetResizable(True)
        self.info_panel = info_panel.InfoPanel(parent=self)
        info_scroll.setWidget(self.info_panel)
        self.splitter.addWidget(info_scroll)
        
        self.splitter.setSizes([self.width() * 0.8, self.width() * 0.2])
        
        self.nodz.signal_NodeMoved.connect(self.on_nodeMoved)
        self.nodz.signal_NodeSelected.connect(self.on_nodeSelected)
        self.nodz.signal_NodeContextMenuEvent.connect(self.node_context_menu)
        self.nodz.signal_KeyPressed.connect(self.pickwalk)
        
        if self.settings.value("splitterSizes"):
            self.splitter.restoreState(self.settings.value("splitterSizes"))
    
    
    def pickwalk(self, key):
        if not self.nodz.scene().selectedItems():
            return
        
        original_sel = self.nodz.scene().selectedItems()
        sel = original_sel[0]
        clear_selection = False
        
        if key == QtCore.Qt.Key_Right:
            plug_names = list(sel.plugs.keys())
            if plug_names:
                plug = plug_names[0]
                for i, conn in enumerate(sel.plugs[plug].connections):
                    parent_node = self.nodz.scene().nodes[conn.socketNode]
                    parent_node.setSelected(True)
                    clear_selection = True
                    break
        
        elif key == QtCore.Qt.Key_Left:
            socket_names = list(sel.sockets.keys())
            if socket_names:
                socket = socket_names[0]
                for i, conn in enumerate(sel.sockets[socket].connections):
                    child_node = self.nodz.scene().nodes[conn.plugNode]
                    child_node.setSelected(True)
                    clear_selection = True
                    break
        
        
        elif key == QtCore.Qt.Key_Up:
            plug_names = list(sel.plugs.keys())
            if plug_names:
                plug = plug_names[0]
                for i, conn in enumerate(sel.plugs[plug].connections):
                    siblings = []
                    for par_conn in conn.socketItem.connections:
                        siblings.append(par_conn.plugNode)
                    cur_index = siblings.index(sel.name)
                    if cur_index == 0:
                        return
                    sibling = siblings[cur_index - 1]
                    child_node = self.nodz.scene().nodes[sibling]
                    child_node.setSelected(True)
                    clear_selection = True
        
        
        elif key == QtCore.Qt.Key_Down:
            plug_names = list(sel.plugs.keys())
            if plug_names:
                plug = plug_names[0]
                for i, conn in enumerate(sel.plugs[plug].connections):
                    siblings = []
                    for par_conn in conn.socketItem.connections:
                        siblings.append(par_conn.plugNode)
                    cur_index = siblings.index(sel.name)
                    if cur_index == len(siblings) - 1:
                        return
                    sibling = siblings[cur_index + 1]
                    child_node = self.nodz.scene().nodes[sibling]
                    child_node.setSelected(True)
                    clear_selection = True
        
        if clear_selection:
            for node in original_sel:
                node.setSelected(False)
    
    
    def on_nodeMoved(self, nodeName, nodePos):
        # print('node {0} moved to {1}'.format(nodeName, nodePos))
        pass
    
    
    def on_nodeSelected(self, selected_nodes):
        if not selected_nodes:
            return
        node = self.get_node_from_name(selected_nodes[0])
        userdata = node.userData
        path = userdata.get('path')
        if path:
            self.info_panel.loadData(path, userdata)
    
    
    def findWindow(self):
        if self.find_win:
            self.find_win.close()
        
        self.find_win = FindNodeWindow(self.nodz, parent=self)
        self.find_win.show()
        self.find_win.activateWindow()
    
    
    def get_node_from_name(self, node_name):
        return self.nodz.scene().nodes[node_name]
    
    
    def node_path(self, node_name):
        node = self.get_node_from_name(node_name)
        userdata = node.userData
        path = userdata.get('path')
        if path:
            clipboard = QtWidgets.QApplication.clipboard()
            clipboard.setText(path)
            print(path)
    
    
    def reveal_file(self, node_name):
        node = self.get_node_from_name(node_name)
        userdata = node.userData
        browsePath = userdata.get('path')
        
        if browsePath:
            pltName = platform.system()
            if pltName == 'Windows':
                browsePath = browsePath.replace('/', '\\')
                os.system("start explorer.exe /select,{}".format(browsePath))
            
            elif pltName == 'Darwin':
                os.system('open -R "{}"'.format(browsePath))
            
            elif pltName == 'Linux':
                os.system('xdg-open "{}"'.format(os.path.dirname(browsePath)))
    
    
    def node_upstream(self, node_name):
        start_node = self.get_node_from_name(node_name)
        connected_nodes = start_node.upstream_nodes()
        
        for node_name in self.nodz.scene().nodes:
            node = self.nodz.scene().nodes[node_name]
            if node in connected_nodes:
                node.setSelected(True)
            else:
                node.setSelected(False)
    
    
    def view_usdfile(self, node_name):
        node = self.get_node_from_name(node_name)
        userdata = node.userData
        path = userdata.get('path')
        layer = Sdf.Layer.FindOrOpen(path)
        if layer:
            win = text_view.TextViewer(input_text=layer.ExportToString(), title=path, parent=self)
            win.show()
    
    
    def view_usdview(self, node_name):
        node = self.get_node_from_name(node_name)
        userdata = node.userData
        path = userdata.get('path')
        worker = threading.Thread(target=launch_usdview, args=[path])
        worker.start()
        
        # subprocess.call(['usdview', path], shell=True)
        # os.system('usdview {}'.format(path))
    
    
    def node_context_menu(self, event, node):
        menu = QtWidgets.QMenu()
        menu.addAction("Copy Node Path", partial(self.node_path, node))
        menu.addAction("Select upstream", partial(self.node_upstream, node))
        menu.addAction("Reveal in filesystem", partial(self.reveal_file, node))
        
        usd_submenu = menu.addMenu("USD")
        usd_submenu.addAction("Inspect layer...", partial(self.view_usdfile, node))
        usd_submenu.addAction("UsdView...", partial(self.view_usdview, node))
        
        tex_submenu = menu.addMenu("Texture")
        
        menu.exec_(event.globalPos())
    
    
    def load_file(self):
        if not self.usdfile:
            return
        
        if not os.path.isfile(self.usdfile):
            raise RuntimeError("Cannot find file: %s" % self.usdfile)
        
        self.nodz.clearGraph()
        self.root_node = None
        self.setWindowTitle('Noodle - {}'.format(self.usdfile))
        
        x = DependencyWalker(self.usdfile)
        x.walk_attributes = self.walk_attributes
        x.start()
        
        # get back the scrubbed initial file path
        # which will let us find the start node properly
        self.usdfile = x.usdfile
        
        nodz_scene = self.nodz.scene()
        
        # pprint(x.nodes)
        nds = []
        for i, node in enumerate(x.nodes):
            
            info = x.nodes[node]
            
            pos = QtCore.QPointF(0, 0)
            node_label = os.path.basename(node)
            
            # node colouring / etc based on the node type
            node_preset = 'node_default'
            node_icon = "sublayer.png"
            if info.get("type") == 'clip':
                node_preset = 'node_clip'
                node_icon = "clip.png"
            elif info.get("type") == 'payload':
                node_preset = 'node_payload'
                node_icon = "payload.png"
            elif info.get("type") == 'variant':
                node_preset = 'node_variant'
                node_icon = "variant.png"
            elif info.get("type") == 'specialize':
                node_preset = 'node_specialize'
                node_icon = "specialize.png"
            elif info.get("type") == 'reference':
                node_preset = 'node_reference'
                node_icon = "reference.png"
            elif info.get("type") == 'tex':
                node_preset = 'node_texture'
                node_icon = "texture.png"
            elif info.get("type") == 'material':
                node_preset = 'node_material'
                node_icon = "material.png"
            
            if not node in nds:
                nodeA = self.nodz.createNode(name=node, label=node_label, preset=node_preset, position=pos)
                if self.usdfile == node:
                    self.root_node = nodeA
                    node_icon = "noodle.png"
                
                icon = QtGui.QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons", node_icon))
                nodeA.icon = icon
                nodeA.setToolTip(node_label)
                
                if nodeA:
                    self.nodz.createAttribute(node=nodeA, name='out', index=0, preset='attr_preset_1',
                                              plug=True, socket=False, dataType=int, socketMaxConnections=-1)
                    
                    nodeA.userData = info
                    
                    if info.get('error', False) is True:
                        self.nodz.createAttribute(node=nodeA, name='ERROR', index=0, preset='attr_preset_2',
                                                  plug=False, socket=False)
                    if info['online'] is False:
                        self.nodz.createAttribute(node=nodeA, name='OFFLINE', index=0, preset='attr_preset_2',
                                                  plug=False, socket=False)
                        # override the node's draw pen with a
                        # lovely red outline
                        nodeA._pen = QtGui.QPen()
                        nodeA._pen.setStyle(QtCore.Qt.SolidLine)
                        nodeA._pen.setWidth(5)
                        nodeA._pen.setColor(QtGui.QColor(255, 0, 0))
                
                nds.append(node)
        
        # pprint(x.edges)
        
        # 'wiring nodes'.center(40, '-')
        # create all the node connections
        for edge in x.edges:
            
            start = edge[0]
            end = edge[1]
            port_type = edge[2]
            try:
                start_node = self.nodz.scene().nodes[start]
                self.nodz.createAttribute(node=start_node, name=port_type, index=-1, preset='attr_preset_1',
                                          plug=False, socket=True, dataType=int, socketMaxConnections=-1)
                # # sort the ports alphabetically
                # start_node.attrs = sorted(start_node.attrs)
                
                self.nodz.createConnection(end, 'out', start, port_type)
            except:
                print('cannot find start node', start)
        
        # layout nodes!
        self.nodz.arrangeGraph(self.root_node)
        # self.nodz.autoLayoutGraph()
        self.nodz._focus()
        
        if x.errored_nodes:
            message = 'Some layers had load errors:\n'
            for errpath in x.errored_nodes:
                message += '{}\n'.format(errpath)
            QtWidgets.QMessageBox.warning(self, 'File Parsing errors', message, QtWidgets.QMessageBox.Ok)
        
        self.file_loaded.emit(self.usdfile)
    
    
    def save_image(self):
        
        multipleFilters = "Image Files (*.jpg *.png) (*.jpg *.png);;All Files (*.*) (*.*)"
        options = QtWidgets.QFileDialog.DontUseNativeDialog
        try:
            # qt 5.2 and up
            options = options | QtWidgets.QFileDialog.DontUseCustomDirectoryIcons
        except:
            pass
        
        filename = QtWidgets.QFileDialog.getSaveFileName(self, 'Save image file', '/', multipleFilters,
                                                         None, options)
        if filename[0]:
            print('saving image:', filename[0])
            
            self.nodz.save_image(filename[0])
    
    
    def layout_nodes(self):
        # layout nodes!
        self.nodz.arrangeGraph(self.root_node)
        # self.nodz.autoLayoutGraph()
        
        self.nodz._focus(all=True)
    
    
    def manualOpen(self):
        """
        Manual open method for manually opening the manually opened files.
        """
        startPath = None
        if self.usdfile:
            startPath = os.path.dirname(self.usdfile)
        
        multipleFilters = "USD Files (*.usd *.usda *.usdc) (*.usd *.usda *.usdc);;All Files (*.*) (*.*)"
        options = QtWidgets.QFileDialog.DontUseNativeDialog
        try:
            # qt 5.2 and up
            options = options | QtWidgets.QFileDialog.DontUseCustomDirectoryIcons
        except:
            pass
        
        filename = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Open File', startPath or '/', multipleFilters,
            None, options)
        if filename[0]:
            print(filename[0])
            self.usdfile = filename[0]
            self.load_file()


def main(usdfile=None, walk_attributes=False):
    par = QtWidgets.QApplication.activeWindow()
    win = NodeGraphWindow(usdfile=usdfile, parent=par, walk_attributes=walk_attributes)
    return win
