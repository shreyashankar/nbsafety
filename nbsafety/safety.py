# -*- coding: future_annotations -*-
import ast
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
import inspect
import logging
import re
from typing import cast, TYPE_CHECKING, NamedTuple

from IPython import get_ipython
from IPython.core.magic import register_cell_magic, register_line_magic

from nbsafety.analysis import (
    compute_live_dead_symbol_refs,
    compute_call_chain_live_symbols,
    get_symbols_for_references,
)
from nbsafety.ipython_utils import (
    ast_transformer_context,
    cell_counter,
    run_cell,
    save_number_of_currently_executing_cell,
)
from nbsafety import line_magics
from nbsafety.data_model.data_symbol import DataSymbol
from nbsafety.data_model.scope import Scope, NamespaceScope
from nbsafety.run_mode import SafetyRunMode
from nbsafety import singletons
from nbsafety.tracing.safety_ast_rewriter import SafetyAstRewriter
from nbsafety.tracing.trace_manager import TraceManager
# from nbsafety.utils.mixins import EnforceSingletonMixin

if TYPE_CHECKING:
    from typing import Any, Dict, Iterable, List, Set, Optional, Tuple, Union
    from types import FrameType
    CellId = Union[str, int]

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

_MAX_WARNINGS = 10
_SAFETY_LINE_MAGIC = 'safety'

_NB_MAGIC_PATTERN = re.compile(r'(^%|^!|^cd |\?$)')


def _safety_warning(node: DataSymbol):
    if not node.is_stale:
        raise ValueError('Expected node with stale ancestor; got %s' % node)
    if node.defined_cell_num < 1:
        return
    fresher_symbols = node.fresher_ancestors
    if len(fresher_symbols) == 0:
        fresher_symbols = node.namespace_stale_symbols
    logger.warning(
        f'`{node.readable_name}` defined in cell {node.defined_cell_num} may depend on '
        f'old version(s) of [{", ".join(f"`{str(dep)}`" for dep in fresher_symbols)}] '
        f'(latest update in cell {node.required_cell_num}).'
        f'\n\n(Run cell again to override and execute anyway.)'
    )


class NotebookSafetySettings(NamedTuple):
    store_history: bool
    test_context: bool
    use_comm: bool
    backwards_cell_staleness_propagation: bool
    track_dependencies: bool
    naive_refresher_computation: bool
    skip_unsafe_cells: bool
    mode: SafetyRunMode


@dataclass
class MutableNotebookSafetySettings:
    trace_messages_enabled: bool
    highlights_enabled: bool


class NotebookSafety(singletons.NotebookSafety):
    """Holds all the state necessary to detect stale dependencies in Jupyter notebooks."""

    def __init__(self, cell_magic_name=None, use_comm=False, **kwargs):
        super().__init__()
        self.settings: NotebookSafetySettings = NotebookSafetySettings(
            store_history=kwargs.pop('store_history', True),
            test_context=kwargs.pop('test_context', False),
            use_comm=use_comm,
            backwards_cell_staleness_propagation=True,
            track_dependencies=True,
            naive_refresher_computation=False,
            skip_unsafe_cells=kwargs.pop('skip_unsafe', True),
            mode=SafetyRunMode.get(),
        )
        self.mut_settings: MutableNotebookSafetySettings = MutableNotebookSafetySettings(
            trace_messages_enabled=kwargs.pop('trace_messages_enabled', False),
            highlights_enabled=kwargs.pop('highlights_enabled', True),
        )
        # Note: explicitly adding the types helps PyCharm intellisense
        self.namespaces: Dict[int, NamespaceScope] = {}
        self.aliases: Dict[int, Set[DataSymbol]] = defaultdict(set)
        self.global_scope: Scope = Scope()
        self.updated_symbols: Set[DataSymbol] = set()
        self.updated_scopes: Set[NamespaceScope] = set()
        self.garbage_namespace_obj_ids: Set[int] = set()
        self.ast_node_by_id: Dict[int, ast.AST] = {}
        self.statement_cache: 'Dict[int, Dict[int, ast.stmt]]' = defaultdict(dict)
        self.cell_content_by_counter: Dict[int, str] = {}
        self.statement_to_func_cell: Dict[int, DataSymbol] = {}
        self.stale_dependency_detected = False
        self.active_cell_position_idx = -1
        self._last_execution_counter = 0
        self._counters_by_cell_id: Dict[CellId, int] = {}
        self._active_cell_id: Optional[str] = None
        if cell_magic_name is None:
            self._cell_magic = None
        else:
            self._cell_magic = self._make_cell_magic(cell_magic_name)
        self._line_magic = self._make_line_magic()
        self._last_refused_code: Optional[str] = None
        self._prev_cell_stale_symbols: Set[DataSymbol] = set()
        self._cell_counter = 1
        self._recorded_cell_name_to_cell_num = True
        self._cell_name_to_cell_num_mapping: Dict[str, int] = {}
        self._ast_transformer_raised: Optional[Exception] = None
        if use_comm:
            get_ipython().kernel.comm_manager.register_target(__package__, self._comm_target)

    @property
    def is_develop(self) -> bool:
        return self.settings.mode == SafetyRunMode.DEVELOP

    @property
    def is_test(self) -> bool:
        return self.settings.test_context

    @property
    def trace_messages_enabled(self) -> bool:
        return self.mut_settings.trace_messages_enabled

    @trace_messages_enabled.setter
    def trace_messages_enabled(self, new_val) -> None:
        self.mut_settings.trace_messages_enabled = new_val

    def get_first_full_symbol(self, obj_id: int) -> Optional[DataSymbol]:
        for alias in self.aliases.get(obj_id, []):
            if not alias.is_anonymous:
                return alias
        return None

    def cell_counter(self):
        if self.settings.store_history:
            return cell_counter()
        else:
            return self._cell_counter

    def reset_cell_counter(self):
        # only called in test context
        assert not self.settings.store_history
        for sym in self.all_data_symbols():
            sym.last_used_cell_num = sym.defined_cell_num = sym.required_cell_num = 0
            sym.version_by_used_timestamp.clear()
            sym.version_by_liveness_timestamp.clear()
        self._cell_counter = 1

    def set_ast_transformer_raised(self, new_val: Optional[Exception] = None) -> Optional[Exception]:
        ret = self._ast_transformer_raised
        self._ast_transformer_raised = new_val
        return ret

    def get_position(self, frame: FrameType):
        try:
            cell_num = self._cell_name_to_cell_num_mapping[frame.f_code.co_filename.split('-')[3]]
            return cell_num, frame.f_lineno
        except KeyError as e:
            print(frame.f_code.co_filename)
            raise e

    def maybe_set_name_to_cell_num_mapping(self, frame: FrameType):
        if self._recorded_cell_name_to_cell_num:
            return
        self._recorded_cell_name_to_cell_num = True
        self._cell_name_to_cell_num_mapping[frame.f_code.co_filename.split('-')[3]] = self.cell_counter()

    def set_active_cell(self, cell_id, position_idx=-1):
        self._active_cell_id = cell_id
        self.active_cell_position_idx = position_idx

    def _comm_target(self, comm, open_msg):
        @comm.on_msg
        def _responder(msg):
            request = msg['content']['data']
            self.handle(request, comm=comm)

        comm.send({'type': 'establish'})

    def handle(self, request, comm=None):
        if request['type'] == 'change_active_cell':
            self.set_active_cell(request['active_cell_id'], position_idx=request.get('active_cell_order_idx', -1))
        elif request['type'] == 'cell_freshness':
            cell_id = request.get('executed_cell_id', None)
            if cell_id is not None:
                self._counters_by_cell_id[cell_id] = self._last_execution_counter
            cells_by_id = request['content_by_cell_id']
            if self.settings.backwards_cell_staleness_propagation:
                order_index_by_id = None
                last_cell_exec_position_idx = -1
            else:
                order_index_by_id = request['order_index_by_cell_id']
                last_cell_exec_position_idx = order_index_by_id.get(cell_id, -1)
            response = self.check_and_link_multiple_cells(cells_by_id, order_index_by_id)
            response['type'] = 'cell_freshness'
            response['last_cell_exec_position_idx'] = last_cell_exec_position_idx
            if comm is not None:
                comm.send(response)
        else:
            logger.error('Unsupported request type for request %s' % request)

    def check_and_link_multiple_cells(
        self,
        cells_by_id: Dict[CellId, str],
        order_index_by_cell_id: Optional[Dict[CellId, int]] = None
    ) -> Dict[str, Any]:
        if not self.mut_settings.highlights_enabled:
            return {
                'stale_cells': [],
                'fresh_cells': [],
                'stale_links': {},
                'refresher_links': {},
            }
        stale_cells = set()
        fresh_cells = []
        stale_symbols_by_cell_id: Dict[CellId, Set[DataSymbol]] = {}
        killing_cell_ids_for_symbol: Dict[DataSymbol, Set[CellId]] = defaultdict(
            set)
        for cell_id, cell_content in cells_by_id.items():
            if (order_index_by_cell_id is not None and
                    order_index_by_cell_id.get(cell_id, -1) <= self.active_cell_position_idx):
                continue
            try:
                symbols = self._check_cell_and_resolve_symbols(cell_content)
                stale_symbols, dead_symbols = symbols['stale'], symbols['dead']
                if len(stale_symbols) > 0:
                    stale_symbols_by_cell_id[cell_id] = stale_symbols
                    stale_cells.add(cell_id)
                elif (self._get_max_defined_cell_num_for_symbols(symbols['live']) >
                      self._counters_by_cell_id.get(cell_id, cast(int, float('inf')))):
                    fresh_cells.append(cell_id)
                for dead_sym in dead_symbols:
                    killing_cell_ids_for_symbol[dead_sym].add(cell_id)
            except SyntaxError:
                continue
        stale_links: Dict[CellId, Set[CellId]] = defaultdict(set)
        refresher_links: Dict[CellId, List[CellId]] = defaultdict(list)
        for stale_cell_id in stale_cells:
            stale_syms = stale_symbols_by_cell_id[stale_cell_id]
            if self.settings.naive_refresher_computation:
                refresher_cell_ids = self._naive_compute_refresher_cells(
                    stale_cell_id,
                    stale_syms,
                    cells_by_id,
                    order_index_by_cell_id=order_index_by_cell_id
                )
            else:
                refresher_cell_ids = set.union(
                    *(killing_cell_ids_for_symbol[stale_sym] for stale_sym in stale_syms))
            stale_links[stale_cell_id] = refresher_cell_ids
        stale_link_changes = True
        # transitive closer up until we hit non-stale refresher cells
        while stale_link_changes:
            stale_link_changes = False
            for stale_cell_id in stale_cells:
                new_stale_links = set(stale_links[stale_cell_id])
                original_length = len(new_stale_links)
                for refresher_cell_id in stale_links[stale_cell_id]:
                    if refresher_cell_id not in stale_cells:
                        continue
                    new_stale_links |= stale_links[refresher_cell_id]
                new_stale_links.discard(stale_cell_id)
                stale_link_changes = stale_link_changes or original_length != len(new_stale_links)
                stale_links[stale_cell_id] = new_stale_links
        for stale_cell_id in stale_cells:
            stale_links[stale_cell_id] -= stale_cells
            for refresher_cell_id in stale_links[stale_cell_id]:
                refresher_links[refresher_cell_id].append(stale_cell_id)
        return {
            'stale_cells': list(stale_cells),
            'fresh_cells': fresh_cells,
            'stale_links': {
                stale_cell_id: list(refresher_cell_ids)
                for stale_cell_id, refresher_cell_ids in stale_links.items()
            },
            'refresher_links': refresher_links,
        }

    def _naive_compute_refresher_cells(
        self,
        stale_cell_id: CellId,
        stale_symbols: Set[DataSymbol],
        cells_by_id: Dict[CellId, str],
        order_index_by_cell_id: Optional[Dict[CellId, int]] = None
    ) -> Set[CellId]:
        refresher_cell_ids: Set[CellId] = set()
        stale_cell_content = cells_by_id[stale_cell_id]
        for cell_id, cell_content in cells_by_id.items():
            if cell_id == stale_cell_id:
                continue
            if (order_index_by_cell_id is not None and
                    order_index_by_cell_id.get(cell_id, -1) >= order_index_by_cell_id.get(stale_cell_id, -1)):
                continue
            concated_content = f'{cell_content}\n\n{stale_cell_content}'
            try:
                concated_stale_symbols = self._check_cell_and_resolve_symbols(concated_content)['stale']
            except SyntaxError:
                continue
            if concated_stale_symbols < stale_symbols:
                refresher_cell_ids.add(cell_id)
        return refresher_cell_ids

    @staticmethod
    def _get_cell_ast(cell):
        lines = []
        for line in cell.strip().split('\n'):
            # TODO: figure out more robust strategy for filtering / transforming lines for the ast parser
            # we filter line magics, but for %time, we would ideally like to trace the statement being timed
            # TODO: how to do this?
            if _NB_MAGIC_PATTERN.search(line) is None:
                lines.append(line)
        return ast.parse('\n'.join(lines))

    def _get_max_defined_cell_num_for_symbols(self, symbols: Set[DataSymbol]) -> int:
        max_defined_cell_num = -1
        for dsym in symbols:
            max_defined_cell_num = max(
                max_defined_cell_num, dsym.defined_cell_num)
            if dsym.obj_id in self.namespaces:
                namespace_scope = self.namespaces[dsym.obj_id]
                max_defined_cell_num = max(
                    max_defined_cell_num, namespace_scope.max_defined_timestamp)
        return max_defined_cell_num

    def _check_cell_and_resolve_symbols(
        self,
        cell: Union[ast.Module, str]
    ) -> Dict[str, Set[DataSymbol]]:
        if isinstance(cell, str):
            cell = self._get_cell_ast(cell)
        live_symbol_refs, dead_symbol_refs = compute_live_dead_symbol_refs(cell)
        live_symbols, called_symbols = get_symbols_for_references(
            live_symbol_refs, self.global_scope)
        live_symbols = live_symbols.union(compute_call_chain_live_symbols(called_symbols))
        # only mark dead attrsubs as killed if we can traverse the entire chain
        dead_symbols, _ = get_symbols_for_references(
            dead_symbol_refs, self.global_scope, only_add_successful_resolutions=True
        )
        stale_symbols = set(dsym for dsym in live_symbols if dsym.is_stale)
        return {
            'live': live_symbols,
            'dead': dead_symbols,
            'stale': stale_symbols,
        }

    def _precheck_for_stale(self, cell: str) -> bool:
        """
        This method statically checks the cell to be executed for stale symbols.
        If any live, stale symbols are detected, it returns `True`. On a syntax
        error or if no such symbols are detected, return `False`.
        Furthermore, if this is the second time the user has attempted to execute
        the exact same code, we assume they want to override this checker and
        we temporarily mark any stale symbols as being not stale and return `False`.
        """
        try:
            cell_ast = self._get_cell_ast(cell)
        except SyntaxError:
            return False
        symbols = self._check_cell_and_resolve_symbols(cell_ast)
        stale_symbols, live_symbols = symbols['stale'], symbols['live']
        if self._last_refused_code is None or cell != self._last_refused_code:
            self._prev_cell_stale_symbols = stale_symbols
            if len(stale_symbols) > 0:
                warning_counter = 0
                for sym in self._prev_cell_stale_symbols:
                    if warning_counter >= _MAX_WARNINGS:
                        logger.warning(f'{len(self._prev_cell_stale_symbols) - warning_counter}'
                                       ' more nodes with stale dependencies skipped...')
                        break
                    _safety_warning(sym)
                    warning_counter += 1
                self.stale_dependency_detected = True
                self._last_refused_code = cell
                return True
        else:
            # Instead of breaking the dependency chain, simply refresh the nodes
            # with stale deps to their required cell numbers
            for sym in self._prev_cell_stale_symbols:
                sym.temporary_disable_warnings()
            self._prev_cell_stale_symbols.clear()

        # For each of the live symbols, record their `defined_cell_num`
        # at the time of liveness, for use with the dynamic slicer.
        for sym in live_symbols:
            sym.version_by_liveness_timestamp[self.cell_counter()] = sym.defined_cell_num

        self._last_refused_code = None
        return False

    def _resync_symbols(self, symbols: Iterable[DataSymbol]):
        for dsym in symbols:
            if not dsym.containing_scope.is_global:
                continue
            obj = get_ipython().user_global_ns.get(dsym.name, None)
            if obj is None:
                continue
            if dsym.obj_id == id(obj):
                continue
            for alias in self.aliases[dsym.cached_obj_id] | self.aliases[dsym.obj_id]:
                if not alias.containing_scope.is_namespace_scope:
                    continue
                containing_scope = cast(NamespaceScope, alias.containing_scope)
                containing_obj = containing_scope.get_obj()
                if containing_obj is None:
                    continue
                # TODO: handle dict case too
                if isinstance(containing_obj, list) and containing_obj[-1] is obj:
                    containing_scope.upsert_data_symbol_for_name(
                        len(containing_obj) - 1,
                        obj,
                        set(alias.parents),
                        alias.stmt_node,
                        is_subscript=True,
                        propagate=False
                    )
            self.aliases[dsym.cached_obj_id].discard(dsym)
            self.aliases[dsym.obj_id].discard(dsym)
            self.aliases[id(obj)].add(dsym)
            namespace = self.namespaces.get(dsym.obj_id, None)
            if namespace is not None:
                namespace.update_obj_ref(obj)
                del self.namespaces[dsym.obj_id]
                self.namespaces[namespace.obj_id] = namespace
            dsym.update_obj_ref(obj)

    def get_cell_dependencies(self, cell_num: int) -> Dict[int, str]:
        """
        Gets a dictionary object of cell dependencies for the last or 
        currently executed cell.

        Args:
            - cell_num (int): cell to get dependencies for, defaults to last
                execution counter

        Returns:
            - dict (int, str): map from required cell number to code
                representing dependencies
        """
        if cell_num not in self.cell_content_by_counter.keys():
            raise ValueError(f'Cell {cell_num} has not been run yet.')

        dependencies: Set[int] = set()
        cell_num_to_dynamic_deps: Dict[int, Set[int]] = defaultdict(set)
        cell_num_to_static_deps: Dict[int, Set[int]] = defaultdict(set)

        for sym in self.all_data_symbols():
            for used_timestamp, version in sym.version_by_used_timestamp.items():
                cell_num_to_dynamic_deps[used_timestamp].add(version)
            for live_timestamp, version in sym.version_by_liveness_timestamp.items():
                cell_num_to_static_deps[live_timestamp].add(version)

        self._get_cell_dependencies(
            cell_num, dependencies, cell_num_to_dynamic_deps, cell_num_to_static_deps)
        return {num: self.cell_content_by_counter[num] for num in dependencies}

    def _get_cell_dependencies(
        self,
        cell_num: int,
        dependencies: Set[int],
        cell_num_to_dynamic_deps: Dict[int, Set[int]],
        cell_num_to_static_deps: Dict[int, Set[int]],
    ) -> None:
        """
        For a given cell, this function recursively populates a set of
        cell numbers that the given cell depends on, based on the live symbols.

        Args:
            - dependencies (set<int>): set of cell numbers so far that exist
            - cell_num (int): current cell to get dependencies for
            - cell_num_to_dynamic_deps (dict<int, set<int>>): mapping from cell 
            num to version of cells where its symbols were used
            - cell_num_to_static_deps (dict<int, set<int>>): mapping from cell 
            num to version of cells where its symbols were defined

        Returns:
            None
        """
        # Base case: cell already in dependencies
        if cell_num in dependencies or cell_num <= 0:
            return

        # Add current cell to dependencies
        dependencies.add(cell_num)

        # Retrieve cell numbers for the dependent symbols
        # Add dynamic and static dependencies
        dep_cell_nums = cell_num_to_dynamic_deps[cell_num] | cell_num_to_static_deps[cell_num]
        logger.info('dynamic cell deps for %d: %s', cell_num,
                    cell_num_to_dynamic_deps[cell_num])
        logger.info('static cell deps for %d: %s', cell_num,
                    cell_num_to_static_deps[cell_num])

        # For each dependent cell, recursively get their dependencies
        for num in dep_cell_nums - dependencies:
            self._get_cell_dependencies(
                num, dependencies, cell_num_to_dynamic_deps, cell_num_to_static_deps)

    def safe_execute(self, cell: str, run_cell_func):
        ret = None
        with save_number_of_currently_executing_cell():
            self._last_execution_counter = self.cell_counter()

            if self._active_cell_id is not None:
                self._counters_by_cell_id[self._active_cell_id] = self._last_execution_counter
                self._active_cell_id = None

            # Stage 1: Precheck.
            if self._precheck_for_stale(cell) and self.settings.skip_unsafe_cells:
                # FIXME: hack to increase cell number
                #  ideally we shouldn't show a cell number at all if we fail precheck since nothing executed
                return run_cell_func('None')

            # Stage 2: Trace / run the cell, updating dependencies as they are encountered.
            try:
                self.cell_content_by_counter[self._last_execution_counter] = cell
                with self._tracing_context():
                    ret = run_cell_func(cell)
                # Stage 2.1: resync any defined symbols that could have gotten out-of-sync
                #  due to tracing being disabled

                self._resync_symbols([
                    # TODO: avoid bad performance by only iterating over symbols updated in this cell
                    sym for sym in self.all_data_symbols() if sym.defined_cell_num == self.cell_counter()
                ])
            finally:
                if not self.settings.store_history:
                    self._cell_counter += 1
                return ret

    def _make_cell_magic(self, cell_magic_name):
        # this is to avoid capturing `self` and creating an extra reference to the singleton
        store_history = self.settings.store_history

        def _run_cell_func(cell):
            run_cell(cell, store_history=store_history)

        def _dependency_safety(_, cell: str):
            singletons.nbs().safe_execute(cell, _run_cell_func)

        # FIXME (smacke): probably not a great idea to rely on this
        _dependency_safety.__name__ = cell_magic_name
        return register_cell_magic(_dependency_safety)

    @contextmanager
    def _tracing_context(self):
        self.updated_symbols.clear()
        self.updated_scopes.clear()
        self._recorded_cell_name_to_cell_num = False

        try:
            with TraceManager.instance().tracing_context():
                with ast_transformer_context([SafetyAstRewriter()]):
                    yield
        finally:
            # TODO: actually handle errors that occurred in our code while tracing
            # if not self.trace_state_manager.error_occurred:
            self._reset_trace_state_hook()

    def _reset_trace_state_hook(self):
        # this assert doesn't hold anymore now that tracing could be disabled inside of something
        # assert len(self.attr_trace_manager.stack) == 0
        TraceManager.clear_instance()
        self._gc()

    def _make_line_magic(self):
        line_magic_names = [f[0] for f in inspect.getmembers(line_magics) if inspect.isfunction(f[1])]

        def _safety(line_: str):
            # this is to avoid capturing `self` and creating an extra reference to the singleton
            try:
                cmd, line = line_.split(' ', 1)
            except ValueError:
                cmd, line = line_, ''
            if cmd in ('deps', 'show_deps', 'show_dependency', 'show_dependencies'):
                return line_magics.show_deps(line)
            elif cmd in ('stale', 'show_stale'):
                return line_magics.show_stale(line)
            elif cmd == 'trace_messages':
                return line_magics.trace_messages(line)
            elif cmd in ('hls', 'nohls', 'highlight', 'highlights'):
                return line_magics.set_highlights(cmd, line)
            elif cmd in ('slice', 'make_slice', 'gather_slice'):
                return line_magics.make_slice(line)
            elif cmd == 'remove_dependency':
                return line_magics.remove_dep(line)
            elif cmd in ('add_dependency', 'add_dep'):
                return line_magics.add_dep(line)
            elif cmd == 'turn_off_warnings_for':
                return line_magics.turn_off_warnings_for(line)
            elif cmd == 'turn_on_warnings_for':
                return line_magics.turn_on_warnings_for(line)
            elif cmd in line_magic_names:
                print('We have a magic for %s, but have not yet registered it' % cmd)
            else:
                print(line_magics.USAGE)

        # FIXME (smacke): probably not a great idea to rely on this
        _safety.__name__ = _SAFETY_LINE_MAGIC
        return register_line_magic(_safety)

    @property
    def dependency_tracking_enabled(self):
        return self.settings.track_dependencies

    @property
    def cell_magic_name(self):
        return self._cell_magic.__name__

    @property
    def line_magic_name(self):
        return self._line_magic.__name__

    def all_data_symbols(self):
        for alias_set in self.aliases.values():
            yield from alias_set

    def test_and_clear_detected_flag(self):
        ret = self.stale_dependency_detected
        self.stale_dependency_detected = False
        return ret

    def _namespace_gc(self):
        for obj_id in self.garbage_namespace_obj_ids:
            garbage_ns = self.namespaces.pop(obj_id, None)
            if garbage_ns is not None:
                logger.info('collect ns %s', garbage_ns)
                garbage_ns.clear_namespace(obj_id)
        self.garbage_namespace_obj_ids.clear()
        # while True:
        #     for obj_id in self.garbage_namespace_obj_ids:
        #         self.namespaces.pop(obj_id, None)
        #     self.garbage_namespace_obj_ids.clear()
        #     for obj_id, namespace in self.namespaces.items():
        #         if namespace.is_garbage:
        #             self.garbage_namespace_obj_ids.add(namespace.obj_id)
        #     if len(self.garbage_namespace_obj_ids) == 0:
        #         break

    def _gc(self):
        for dsym in list(self.all_data_symbols()):
            if dsym.is_garbage:
                logger.info('collect sym %s', dsym)
                dsym.collect_self_garbage()

    def retrieve_namespace_attr_or_sub(self, obj: Any, attr_or_sub: Union[str, int], is_subscript: bool):
        try:
            if is_subscript:
                # TODO: more complete list of things that are checkable
                #  or could cause side effects upon subscripting
                return obj[attr_or_sub]
            else:
                if self.is_develop:
                    assert isinstance(attr_or_sub, str)
                return getattr(obj, cast(str, attr_or_sub))
        except (AttributeError, IndexError, KeyError):
            raise
        except Exception as e:
            if self.is_develop:
                logger.warning('unexpected exception: %s', e)
                logger.warning('object: %s', obj)
                logger.warning('attr / subscript: %s', attr_or_sub)
            raise e
