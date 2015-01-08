'''
    Compiler classes for Cheetah:
    Compiler
    ClassCompiler
    MethodCompiler

    If you are trying to grok this code start with Compiler.__init__,
    Compiler.compile, and Compiler.__getattr__.
'''
from __future__ import unicode_literals

import collections
import contextlib
import copy
import re
import textwrap
import warnings

import six

from Cheetah.ast_utils import get_imported_names
from Cheetah.ast_utils import get_lvalues
from Cheetah.legacy_parser import escapedNewlineRE
from Cheetah.legacy_parser import LegacyParser
from Cheetah.SettingsManager import SettingsManager


CallDetails = collections.namedtuple(
    'CallDetails', ['call_id', 'function_name', 'args', 'lineCol'],
)

INDENT = 4 * ' '


BUILTIN_NAMES = frozenset(dir(six.moves.builtins))


# Settings format: (key, default, docstring)
_DEFAULT_COMPILER_SETTINGS = [
    ('useNameMapper', True, 'Enable NameMapper for dotted notation and searchList support'),
    ('useAutocalling', False, 'Detect and call callable objects in searchList, requires useNameMapper=True'),
    ('useDottedNotation', False, 'Allow use of dotted notation for dictionary lookups, requires useNameMapper=True'),
    ('useLegacyImportMode', True, 'All #import statements are relocated to the top of the generated Python module'),
    ('mainMethodName', 'respond', ''),
    ('mainMethodNameForSubclasses', 'writeBody', ''),
    ('gettextTokens', ['_', 'gettext', 'ngettext', 'pgettext', 'npgettext'], ''),
    ('macroDirectives', {}, 'For providing macros'),
    ('optimize_lookup', True, ''),
]

DEFAULT_COMPILER_SETTINGS = dict((v[0], v[1]) for v in _DEFAULT_COMPILER_SETTINGS)

CLASS_NAME = 'YelpCheetahTemplate'
BASE_CLASS_NAME = 'YelpCheetahBaseClass'


def genPlainVar(nameChunks):
    """Generate Python code for a Cheetah $var without using NameMapper."""
    nameChunks.reverse()
    chunk = nameChunks.pop()
    pythonCode = chunk[0] + chunk[2]
    while nameChunks:
        chunk = nameChunks.pop()
        pythonCode = pythonCode + '.' + chunk[0] + chunk[2]
    return pythonCode


def _arg_chunk_to_text(chunk):
    if chunk[1] is not None:
        return '{0}={1}'.format(*chunk)
    else:
        return chunk[0]


def arg_string_list_to_text(arg_string_list):
    return ', '.join(_arg_chunk_to_text(chunk) for chunk in arg_string_list)


class MethodCompiler(object):
    def __init__(
            self,
            methodName,
            class_compiler,
            initialMethodComment,
            decorators=None,
    ):
        self._next_variable_id = 0
        self._methodName = methodName
        self._initialMethodComment = initialMethodComment
        self._indentLev = 2
        self._pendingStrConstChunks = []
        self._methodBodyChunks = []
        self._callRegionsStack = []
        self._filterRegionsStack = []
        self._hasReturnStatement = False
        self._isGenerator = False
        self._arguments = [('self', None)]
        self._local_vars = set(('self',))
        self._decorators = decorators or []

    def cleanupState(self):
        """Called by the containing class compiler instance"""
        self.commitStrConst()

        self._indentLev = 2
        mainBodyChunks = self._methodBodyChunks
        self._methodBodyChunks = []
        self._addAutoSetupCode()
        self._methodBodyChunks.extend(mainBodyChunks)
        self._addAutoCleanupCode()

    def methodName(self):
        return self._methodName

    def setMethodName(self, name):
        self._methodName = name

    # methods for managing indentation

    def indentation(self):
        return INDENT * self._indentLev

    def indent(self):
        self._indentLev += 1

    def dedent(self):
        if not self._indentLev:
            raise AssertionError('Attempt to dedent when the indentLev is 0')
        self._indentLev -= 1

    # methods for final code wrapping

    def methodDef(self):
        self.commitStrConst()
        return self.methodSignature() + ''.join(self._methodBodyChunks)

    # methods for adding code

    def addChunk(self, chunk=''):
        self.commitStrConst()
        if chunk:
            chunk = '\n' + self.indentation() + chunk
        else:
            chunk = '\n'
        self._methodBodyChunks.append(chunk)

    def appendToPrevChunk(self, appendage):
        self._methodBodyChunks[-1] += appendage

    def addWriteChunk(self, chunk):
        self.addChunk('write({0})'.format(chunk))

    def addFilteredChunk(self, chunk, rawExpr=None, lineCol=None):
        if rawExpr and rawExpr.find('\n') == -1 and rawExpr.find('\r') == -1:
            self.addChunk('_v = {0} # {1!r}'.format(chunk, rawExpr))
            self.appendToPrevChunk(' on line %s, col %s' % lineCol)
        else:
            self.addChunk('_v = %s' % chunk)

        self.addChunk('if _v is not NO_CONTENT: write(_filter(_v))')

    def addStrConst(self, strConst):
        self._pendingStrConstChunks.append(strConst)

    def commitStrConst(self):
        """Add the code for outputting the pending strConst without chopping off
        any whitespace from it.
        """
        if not self._pendingStrConstChunks:
            return

        strConst = ''.join(self._pendingStrConstChunks)
        self._pendingStrConstChunks = []
        if not strConst:
            return

        reprstr = repr(strConst).lstrip('u')
        body = escapedNewlineRE.sub('\\1\n', reprstr[1:-1])

        if reprstr[0] == "'":
            out = ("'''", body, "'''")
        else:
            out = ('"""', body, '"""')
        self.addWriteChunk(''.join(out))

    def handleWSBeforeDirective(self):
        """Truncate the pending strConst to the beginning of the current line.
        """
        if self._pendingStrConstChunks:
            src = self._pendingStrConstChunks[-1]
            BOL = max(src.rfind('\n') + 1, src.rfind('\r') + 1, 0)
            if BOL < len(src):
                self._pendingStrConstChunks[-1] = src[:BOL]

    def addComment(self, comment):
        comment = comment.rstrip('\n')
        self.addChunk('#' + comment)

    def _append_line_col_comment(self, line_col):
        self.appendToPrevChunk(' # generated from line {0}, col {1}.'.format(
            *line_col
        ))

    def _update_locals(self, expr):
        self._local_vars.update(get_lvalues(expr))

    def addPlaceholder(self, expr, rawPlaceholder, line_col):
        self.addFilteredChunk(expr, rawPlaceholder, line_col)
        self._append_line_col_comment(line_col)

    def _add_with_line_col(self, expr, line_col):
        self._update_locals(expr)
        self.addChunk(expr)
        self._append_line_col_comment(line_col)

    addSet = addSilent = addPy = addPass = addDel = _add_with_line_col
    addAssert = addRaise = addBreak = addContinue = _add_with_line_col

    def addReturn(self, expr, line_col):
        assert not self._isGenerator
        self._hasReturnStatement = True
        self._add_with_line_col(expr, line_col)

    def addYield(self, expr, line_col):
        assert not self._hasReturnStatement
        self._isGenerator = True
        self._add_with_line_col(expr, line_col)

    def _add_indenting_directive(self, expr, line_col):
        assert expr[-1] != ':'
        expr = expr + ':'
        self.addChunk(expr)
        self._append_line_col_comment(line_col)
        self.indent()

    addWhile = addIf = addTry = _add_indenting_directive

    def _add_lvalue_indenting_directive(self, expr, line_col):
        self._update_locals(expr + ':\n    pass')
        self._add_indenting_directive(expr, line_col)

    addFor = addWith = _add_lvalue_indenting_directive

    def addReIndentingDirective(self, expr, line_col, dedent=True):
        self.commitStrConst()
        if dedent:
            self.dedent()
        assert expr[-1] != ':'
        expr = expr + ':'

        self.addChunk(expr)
        self._append_line_col_comment(line_col)
        self.indent()

    addFinally = addReIndentingDirective

    def addExcept(self, expr, line_col, dedent=True):
        self._update_locals('try:\n    pass\n' + expr + ':\n    pass')
        self.addReIndentingDirective(expr, line_col, dedent=dedent)

    def addElse(self, expr, line_col, dedent=True):
        expr = re.sub('else +if', 'elif', expr)
        self.addReIndentingDirective(expr, line_col, dedent=dedent)

    addElif = addElse

    def next_id(self):
        self._next_variable_id += 1
        return '_{0}'.format(self._next_variable_id)

    def startCallRegion(self, function_name, args, lineCol):
        call_id = self.next_id()
        call_details = CallDetails(call_id, function_name, args, lineCol)
        self._callRegionsStack.append(call_details)

        self.addChunk(
            '## START CALL REGION: {call_id} of {function_name} '
            'at line {line}, col {col}.'.format(
                call_id=call_id,
                function_name=function_name,
                line=lineCol[0],
                col=lineCol[1],
            )
        )
        self.addChunk('_orig_trans{0} = trans'.format(call_id))
        self.addChunk(
            'self.transaction = trans = _call{0} = DummyTransaction()'.format(
                call_id
            )
        )
        self.addChunk('write = trans.write')

    def endCallRegion(self):
        call_details = self._callRegionsStack.pop()
        call_id, function_name, args, (line, col) = (
            call_details.call_id,
            call_details.function_name,
            call_details.args,
            call_details.lineCol,
        )

        self.addChunk(
            'self.transaction = trans = _orig_trans{0}'.format(call_id),
        )
        self.addChunk('write = trans.write')
        self.addChunk('del _orig_trans{0}'.format(call_id))

        self.addChunk('_call_arg{0} = _call{0}.getvalue()'.format(call_id))
        self.addChunk('del _call{0}'.format(call_id))

        args = (', ' + args).strip()
        self.addFilteredChunk(
            '{function_name}(_call_arg{call_id}{args})'.format(
                function_name=function_name,
                call_id=call_id,
                args=args,
            )
        )
        self.addChunk('del _call_arg{0}'.format(call_id))
        self.addChunk(
            '## END CALL REGION: {call_id} of {function_name} '
            'at line {line}, col {col}.'.format(
                call_id=call_id,
                function_name=function_name,
                line=line,
                col=col,
            )
        )
        self.addChunk()

    def setFilter(self, filter_name):
        filter_id = self.next_id()
        self._filterRegionsStack.append(filter_id)

        self.addChunk('_orig_filter{0} = _filter'.format(filter_id))
        if filter_name.lower() == 'none':
            self.addChunk('_filter = self._CHEETAH__initialFilter')
        else:
            self.addChunk(
                '_filter = '
                'self._CHEETAH__currentFilter = '
                'self._CHEETAH__filters[{0!r}]'.format(filter_name)
            )

    def closeFilterBlock(self):
        filter_id = self._filterRegionsStack.pop()
        self.addChunk(
            '_filter = self._CHEETAH__currentFilter = _orig_filter{0}'.format(
                filter_id,
            )
        )

    def _addAutoSetupCode(self):
        self.addChunk(self._initialMethodComment)

        self.addChunk('trans = self.transaction')
        self.addChunk('if not trans:')
        self.indent()
        self.addChunk('self.transaction = trans = DummyTransaction()')
        self.addChunk('_dummyTrans = True')
        self.dedent()
        self.addChunk('else:')
        self.indent()
        self.addChunk('_dummyTrans = False')
        self.dedent()
        self.addChunk('write = trans.write')
        self.addChunk('SL = self._CHEETAH__searchList')
        self.addChunk('_filter = self._CHEETAH__currentFilter')
        self.addChunk()
        self.addChunk('## START - generated method body')
        self.addChunk()

    def _addAutoCleanupCode(self):
        self.addChunk()
        self.addChunk('## END - generated method body')

        if not self._isGenerator:
            self.addChunk()
            self.addChunk('if _dummyTrans:')
            self.indent()
            self.addChunk('self.transaction = None')
            self.addChunk('return trans.getvalue()')
            self.dedent()
            self.addChunk('else:')
            self.indent()
            self.addChunk('return NO_CONTENT')
            self.dedent()

    def addMethArg(self, name, val):
        self._arguments.append((name, val))
        self._local_vars.add(name.lstrip('*'))

    def methodSignature(self):
        arg_text = arg_string_list_to_text(self._arguments)
        return ''.join((
            ''.join(
                INDENT + decorator + '\n' for decorator in self._decorators
            ),
            INDENT + 'def ' + self.methodName() + '(' + arg_text + '):'
        ))


class ClassCompiler(object):
    methodCompilerClass = MethodCompiler

    def __init__(self, main_method_name):
        self._mainMethodName = main_method_name
        self._decoratorsForNextMethod = []
        self._activeMethodsList = []        # stack while parsing/generating
        self._attrs = []
        self._finishedMethodsList = []      # store by order

        self._main_method = self._spawnMethodCompiler(
            main_method_name,
            '## CHEETAH: main method generated for this template'
        )

    def __getattr__(self, name):
        """Provide access to the methods and attributes of the MethodCompiler
        at the top of the activeMethods stack: one-way namespace sharing
        """
        return getattr(self._activeMethodsList[-1], name)

    def cleanupState(self):
        while self._activeMethodsList:
            methCompiler = self._popActiveMethodCompiler()
            self._swallowMethodCompiler(methCompiler)

    def setMainMethodName(self, methodName):
        self._main_method.setMethodName(methodName)

    def _spawnMethodCompiler(self, methodName, initialMethodComment):
        methodCompiler = self.methodCompilerClass(
            methodName,
            class_compiler=self,
            initialMethodComment=initialMethodComment,
            decorators=self._decoratorsForNextMethod,
        )
        self._decoratorsForNextMethod = []
        self._activeMethodsList.append(methodCompiler)
        return methodCompiler

    def _getActiveMethodCompiler(self):
        return self._activeMethodsList[-1]

    def _popActiveMethodCompiler(self):
        return self._activeMethodsList.pop()

    def _swallowMethodCompiler(self, methodCompiler):
        methodCompiler.cleanupState()
        self._finishedMethodsList.append(methodCompiler)
        return methodCompiler

    def startMethodDef(self, methodName, argsList, parserComment):
        methodCompiler = self._spawnMethodCompiler(
            methodName, parserComment,
        )
        for argName, defVal in argsList:
            methodCompiler.addMethArg(argName, defVal)

    def addDecorator(self, decorator_expr):
        """Set the decorator to be used with the next method in the source.

        See _spawnMethodCompiler() and MethodCompiler for the details of how
        this is used.
        """
        self._decoratorsForNextMethod.append(decorator_expr)

    def addAttribute(self, attr_expr):
        self._attrs.append(attr_expr)

    def addSuper(self, argsList):
        methodName = self._getActiveMethodCompiler().methodName()
        arg_text = arg_string_list_to_text(argsList)
        self.addFilteredChunk(
            'super({0}, self).{1}({2})'.format(
                CLASS_NAME, methodName, arg_text,
            )
        )

    def closeDef(self):
        self.commitStrConst()
        methCompiler = self._popActiveMethodCompiler()
        self._swallowMethodCompiler(methCompiler)

    def closeBlock(self):
        self.commitStrConst()
        methCompiler = self._popActiveMethodCompiler()
        methodName = methCompiler.methodName()
        self._swallowMethodCompiler(methCompiler)

        # insert the code to call the block
        self.addChunk('self.{0}()'.format(methodName))

    def class_def(self):
        return '\n'.join((
            'class {0}({1}):\n'.format(CLASS_NAME, BASE_CLASS_NAME),
            self.attributes(),
            self.methodDefs(),
        ))

    def methodDefs(self):
        return '\n\n'.join(
            method.methodDef() for method in self._finishedMethodsList
        )

    def attributes(self):
        if self._attrs:
            return '\n'.join(INDENT + attr for attr in self._attrs) + '\n'
        else:
            return ''


class LegacyCompiler(SettingsManager):
    parserClass = LegacyParser
    classCompilerClass = ClassCompiler

    def __init__(self, source, settings=None):
        super(LegacyCompiler, self).__init__()
        # Important for our compiler which finds function definitions
        self._original_source = source
        self._original_settings = settings or {}

        self.updateSettings(self._original_settings)

        assert isinstance(source, six.text_type), 'the yelp-cheetah compiler requires text, not bytes.'

        if source == '':
            warnings.warn('You supplied an empty string for the source!')

        self._parser = self.parserClass(source, compiler=self)
        self._class_compiler = None
        self._base_import = 'from Cheetah.Template import {0} as {1}'.format(
            CLASS_NAME, BASE_CLASS_NAME,
        )
        self._importStatements = [
            'from Cheetah.DummyTransaction import DummyTransaction',
            'from Cheetah.NameMapper import valueForName as VFN',
            'from Cheetah.NameMapper import valueFromFrameOrSearchList as VFFSL',
            'from Cheetah.Template import NO_CONTENT',
        ]
        self._global_vars = set((
            'DummyTransaction', 'NO_CONTENT', 'VFN', 'VFFSL',
        ))

        self._gettext_scannables = []

    def __getattr__(self, name):
        """Provide one-way access to the methods and attributes of the
        ClassCompiler, and thereby the MethodCompilers as well.
        """
        return getattr(self._class_compiler, name)

    def _initializeSettings(self):
        self._settings = copy.deepcopy(DEFAULT_COMPILER_SETTINGS)

    def _spawnClassCompiler(self):
        return self.classCompilerClass(
            main_method_name=self.setting('mainMethodName'),
        )

    @contextlib.contextmanager
    def _set_class_compiler(self, class_compiler):
        orig = self._class_compiler
        self._class_compiler = class_compiler
        try:
            yield
        finally:
            self._class_compiler = orig

    def addImportedVarNames(self, varNames, raw_statement=None):
        if not varNames:
            return
        if not self.setting('useLegacyImportMode'):
            if raw_statement and getattr(self, '_methodBodyChunks'):
                self.addChunk(raw_statement)
        else:
            self._global_vars.update(varNames)

    # methods for adding stuff to the module and class definitions

    def genCheetahVar(self, nameChunks, lineCol, plain=False):
        first_accessed_var = nameChunks[0][0].partition('.')[0]
        optimize_enabled = (
            self.setting('optimize_lookup') and
            not self.setting('useAutocalling') and
            not self.setting('useDottedNotation')
        )
        plain = (
            not self.setting('useNameMapper') or
            plain or (
                optimize_enabled and (
                    first_accessed_var in self._local_vars or
                    first_accessed_var in self._global_vars or
                    first_accessed_var in BUILTIN_NAMES
                )
            )
        )

        # Look for gettext tokens within nameChunks (if any)
        if any(nameChunk[0] in self.setting('gettextTokens') for nameChunk in nameChunks):
            self.addGetTextVar(nameChunks, lineCol)

        if plain:
            return genPlainVar(nameChunks)
        else:
            return self.genNameMapperVar(
                nameChunks, optimize_enabled=optimize_enabled,
            )

    def addGetTextVar(self, nameChunks, lineCol):
        """Output something that gettext can recognize.

        This is a harmless side effect necessary to make gettext work when it
        is scanning compiled templates for strings marked for translation.
        """
        scannable = genPlainVar(nameChunks[:])
        scannable += ' # generated from line {0}, col {1}.'.format(*lineCol)
        self._gettext_scannables.append(scannable)

    def genNameMapperVar(self, nameChunks, optimize_enabled):
        """Generate valid Python code for a Cheetah $var, using NameMapper
        (Unified Dotted Notation with the SearchList).

        nameChunks = list of var subcomponents represented as tuples
          [(name, useAC, remainderOfExpr)...]
        where:
          name = the dotted name base
          useAC = where NameMapper should use autocalling on namemapperPart
          remainderOfExpr = any arglist, index, or slice

        If remainderOfExpr contains a call arglist (e.g. '(1234)') then useAC
        is False, otherwise it defaults to True. It is overridden by the global
        setting 'useAutocalling' if this setting is False.

        EXAMPLE
        ------------------------------------------------------------------------
        if the raw Cheetah Var is
          $a.b.c[1].d().x.y.z

        nameChunks is the list
          [ ('a.b.c',True,'[1]'), # A
            ('d',False,'()'),     # B
            ('x.y.z',True,''),    # C
          ]

        When this method is fed the list above it returns
          VFN(VFN(VFFSL(SL, 'a.b.c',True)[1], 'd',False)(), 'x.y.z',True)
        which can be represented as
          VFN(B`, name=C[0], executeCallables=(useAC and C[1]))C[2]
        where:
          VFN = NameMapper.valueForName
          VFFSL = NameMapper.valueFromFrameOrSearchList
          SL = self.searchList()
          useAC = self.setting('useAutocalling') # True in this example

          A = ('a.b.c',True,'[1]')
          B = ('d',False,'()')
          C = ('x.y.z',True,'')

          C` = VFN( VFN( VFFSL(SL, 'a.b.c',True)[1],
                         'd',False)(),
                    'x.y.z',True)
             = VFN(B`, name='x.y.z', executeCallables=True)

          B` = VFN(A`, name=B[0], executeCallables=(useAC and B[1]))B[2]
          A` = VFFSL(SL, name=A[0], executeCallables=(useAC and A[1]))A[2]
        """
        defaultUseAC = self.setting('useAutocalling')
        useDottedNotation = self.setting('useDottedNotation')

        nameChunks.reverse()
        name, useAC, remainder = nameChunks.pop()

        if optimize_enabled:
            namept1, dot, rest = name.partition('.')
            pythonCode = 'VFFSL(SL, "{0}"){1}{2}{3}'.format(
                namept1, dot, rest, remainder,
            )
        else:
            pythonCode = 'VFFSL(SL, "%s", %s, %s)%s' % (
                name,
                defaultUseAC and useAC,
                useDottedNotation,
                remainder,
            )

        while nameChunks:
            name, useAC, remainder = nameChunks.pop()
            useAC = defaultUseAC and useAC

            if optimize_enabled:
                pythonCode = '{0}.{1}{2}'.format(pythonCode, name, remainder)
            else:
                pythonCode = 'VFN(%s, "%s", %s, %s)%s' % (
                    pythonCode,
                    name,
                    useAC,
                    useDottedNotation,
                    remainder,
                )

        return pythonCode

    def set_extends(self, extends_name):
        self.setMainMethodName(self.setting('mainMethodNameForSubclasses'))

        if extends_name in self._global_vars:
            raise AssertionError(
                'yelp_cheetah only supports extends by module name'
            )

        self._base_import = 'from {0} import {1} as {2}'.format(
            extends_name, CLASS_NAME, BASE_CLASS_NAME,
        )

        # TODO(#183): stop using the metaclass and just generate functions
        # Partial templates expose their functions as globals, find all the
        # defined functions and add them to known global vars.
        if extends_name == 'Cheetah.partial_template':
            self._global_vars.update(get_defined_method_names(
                self._original_source, self._original_settings,
            ))

    def setCompilerSettings(self, settingsStr):
        self.updateSettingsFromConfigStr(settingsStr)

    def _add_import_statement(self, imp_statement, line_col):
        imported_names = get_imported_names(imp_statement)

        if not self._methodBodyChunks or self.setting('useLegacyImportMode'):
            # In the case where we are importing inline in the middle of a
            # source block we don't want to inadvertantly import the module at
            # the top of the file either
            self._importStatements.append(imp_statement)
        self.addImportedVarNames(imported_names, raw_statement=imp_statement)

    addFrom = addImport = _add_import_statement

    # methods for module code wrapping

    def getModuleCode(self):
        class_compiler = self._spawnClassCompiler()
        with self._set_class_compiler(class_compiler):
            self._parser.parse()
            class_compiler.cleanupState()

        moduleDef = textwrap.dedent(
            """
            from __future__ import unicode_literals
            {imports}
            {base_import}


            # This is compiled yelp_cheetah sourcecode
            __YELP_CHEETAH__ = True


            {class_def}

            {scannables}
            if __name__ == '__main__':
                from os import environ
                from sys import stdout
                stdout.write({class_name}(searchList=[environ]).respond())
            """
        ).strip().format(
            imports='\n'.join(self._importStatements),
            base_import=self._base_import,
            class_def=class_compiler.class_def(),
            scannables=self.gettext_scannables(),
            class_name=CLASS_NAME,
        ) + '\n'

        return moduleDef

    def gettext_scannables(self):
        scannables = tuple(INDENT + nameChunks for nameChunks in self._gettext_scannables)
        if scannables:
            return '\n'.join(
                ('\ndef __CHEETAH_gettext_scannables():',) + scannables
            ) + '\n\n'
        else:
            return ''


def get_defined_method_names(original_source, original_settings):
    class CollectsMethodNamesCompiler(SettingsManager):
        def __init__(self):
            super(CollectsMethodNamesCompiler, self).__init__()
            self.updateSettings(original_settings)
            self.method_names = set()

        # Implement SettingsManager
        def _initializeSettings(self):
            self._settings = copy.deepcopy(DEFAULT_COMPILER_SETTINGS)

        # Trivially allow anything outside of startMethodDef
        def __getattr__(self, name):
            return lambda *args, **kwargs: ''

        # Collect our function names
        def startMethodDef(self, method_name, *args):
            self.method_names.add(method_name)

    compiler = CollectsMethodNamesCompiler()
    parser = LegacyParser(original_source, compiler=compiler)
    parser.parse()
    return compiler.method_names
