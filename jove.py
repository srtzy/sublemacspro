# REMIND: should_reset_target_column should be implemented as state in the view, which is set to
# true until the first time next-line and prev-line are called (assuming we can trap that). That way
# we don't do it after each command but only just before we issue a next/prev line command.

import re, sys, time, os
import functools as fu
import sublime, sublime_plugin
from copy import copy

from sublemacspro.lib.misc import *
from sublemacspro.lib.kill_ring import *
from sublemacspro.lib.isearch import *

import Default.paragraph as paragraph
from . import sbp_layout as ll

# repeatable commands
repeatable_cmds = set(['move', 'left_delete', 'right_delete', 'undo', 'redo'])

class ViewWatcher(sublime_plugin.EventListener):
    def __init__(self, *args, **kwargs):
        super(ViewWatcher, self).__init__(*args, **kwargs)
        self.pending_dedups = 0

    def on_close(self, view):
        ViewState.on_view_closed(view)

    def on_modified(self, view):
        CmdUtil(view).toggle_active_mark_mode(False)

    def on_activated_async(self, view):
        info = isearch_info_for(view)
        if info and not view.settings().get("is_widget"):
            # stop the search if we activated a new view in this window
            info.done()

    def on_query_context(self, view, key, operator, operand, match_all):
        def test(a):
            if operator == sublime.OP_EQUAL:
                return a == operand
            if operator == sublime.OP_NOT_EQUAL:
                return a != operand
            return False

        if key == "i_search_active":
            return test(isearch_info_for(view) is not None)
        if key == "sbp_has_visible_mark":
            if not settings_helper.get("sbp_cancel_mark_enabled", False):
                return False
            return CmdUtil(view).state.mark_ring.has_visible_mark() == operand
        if key == "sbp_use_alt_bindings":
            return test(settings_helper.get("sbp_use_alt_bindings"))
        if key == "sbp_use_super_bindings":
            return test(settings_helper.get("sbp_use_super_bindings"))
        if key == "sbp_alt+digit_inserts":
            return test(settings_helper.get("sbp_alt+digit_inserts") or not settings_helper.get("sbp_use_alt_bindings"))
        if key == 'sbp_has_prefix_argument':
            return test(CmdUtil(view).has_prefix_arg())

    def on_post_save(self, view):
        # Schedule a dedup, but do not do it NOW because it seems to cause a crash if, say, we're
        # saving all the buffers right now. So we schedule it for the future.
        self.pending_dedups += 1
        def doit():
            self.pending_dedups -= 1
            if self.pending_dedups == 0:
                dedup_views(sublime.active_window())
        sublime.set_timeout(doit, 50)

#
# CmdWatcher watches all the commands and tries to correctly process the following situations:
#
#   - canceling i-search if another window command is performed or a mouse drag starts
#   - override commands and run them N times if there is a numeric argument supplied
#   - if transient mark mode, automatically extend the mark when using certain commands like forward
#     word or character
#
class CmdWatcher(sublime_plugin.EventListener):
    def __init__(self, *args, **kwargs):
        super(CmdWatcher, self).__init__(*args, **kwargs)

    def on_anything(self, view):
        view.erase_status(JOVE_STATUS)

    def on_window_command(self, window, cmd, args):
        # Some window commands take us to new view. Here's where we abort the isearch if that happens.
        info = isearch_info_for(window)
        def check():
            if info is not None and window.active_view() != info.view:
                info.done()
        if info is not None:
            sublime.set_timeout(check, 0)

    #
    # Override some commands to execute them N times if the numberic argument is supplied.
    #
    def on_text_command(self, view, cmd, args):
        if isearch_info_for(view) is not None:
            if cmd not in ('sbp_inc_search', 'sbp_inc_search_escape'):
                return ('sbp_inc_search_escape', {'next_cmd': cmd, 'next_args': args})
            return

        vs = ViewState.get(view)
        self.on_anything(view)

        if args is None:
            args = {}


        # first keep track of this_cmd and last_cmd (if command starts with "sbp_" it's handled
        # elsewhere)
        if not cmd.startswith("sbp_"):
            vs.this_cmd = cmd

        #
        #  Process events that create a selection. The hard part is making it work with the emacs region.
        #
        if cmd == 'drag_select':
            info = isearch_info_for(view)
            if info:
                info.done()

            # Set drag_count to 0 when drag_select command occurs. BUT, if the 'by' parameter is
            # present, that means a double or triple click occurred. When that happens we have a
            # selection we want to start using, so we set drag_count to 2. 2 is the number of
            # drag_counts we need in the normal course of events before we turn on the active mark
            # mode.
            vs.drag_count = 2 if 'by' in args else 0

        if cmd in ('move', 'move_to') and vs.active_mark and not args.get('extend', False):
            # this is necessary or else the built-in commands (C-f, C-b) will not move when there is
            # an existing selection
            args['extend'] = True
            return (cmd, args)

        # now check for numeric argument and rewrite some commands as necessary
        if not vs.argument_supplied:
            return None

        if cmd in repeatable_cmds:
            count = vs.get_count()
            args.update({
                'cmd': cmd,
                '_times': abs(count),
            })
            if count < 0 and 'forward' in args:
                args['forward'] = not args['forward']
            return ("sbp_do_times", args)
        elif cmd == 'scroll_lines':
            args['amount'] *= vs.get_count()
            return (cmd, args)

    #
    # Post command processing: deal with active mark and resetting the numeric argument.
    #
    def on_post_text_command(self, view, cmd, args):
        vs = ViewState.get(view)
        cm = CmdUtil(view)
        if vs.active_mark and vs.this_cmd != 'drag_select' and vs.last_cmd == 'drag_select':
            # if we just finished a mouse drag, make sure active mark mode is off
            if cmd != "context_menu":
                cm.toggle_active_mark_mode(False)

        # reset numeric argument (if command starts with "sbp_" this is handled elsewhere)
        if not cmd.startswith("sbp_"):
            vs.argument_value = 0
            vs.argument_supplied = False
            vs.last_cmd = cmd

        if vs.active_mark:
            cm.set_cursors(cm.get_regions())

        # if vs.active_mark:
        #     if len(view.sel()) > 1:
        #         # allow the awesomeness of multiple cursors to be used: the selection will disappear
        #         # after the next command
        #         vs.active_mark = False
        #     else:
        #         cm.set_selection(cm.get_mark(), cm.get_point())

        if cmd in ensure_visible_cmds and cm.just_one_cursor():
            cm.ensure_visible(cm.get_last_cursor())

    #
    # Process the selection if it was created from a drag_select (mouse dragging) command.
    #
    def on_selection_modified(self, view):
        vs = ViewState.get(view)
        selection = view.sel()

        if len(selection) == 1 and vs.this_cmd == 'drag_select':
            cm = CmdUtil(view, vs);
            if vs.drag_count == 2:
                # second event - enable active mark
                region = view.sel()[0]
                cm.set_mark([sublime.Region(region.a)], and_selection=False)
                cm.toggle_active_mark_mode(True)
            elif vs.drag_count == 0:
                cm.toggle_active_mark_mode(False)
        vs.drag_count += 1


    #
    # At a minimum this is called when bytes are inserted into the buffer.
    #
    def on_modified(self, view):
        ViewState.get(view).this_cmd = None
        self.on_anything(view)


class WindowCmdWatcher(sublime_plugin.EventListener):

    def __init__(self, *args, **kwargs):
        super(WindowCmdWatcher, self).__init__(*args, **kwargs)


    def on_window_command(self, window, cmd, args):
        # REMIND - JP: Why is this code here? Can't this be done in the SbpPaneCmd class?

        # Check the move state of the Panes and make sure we stop recursion
        if cmd == "sbp_pane_cmd" and args and args['cmd'] == 'move' and 'next_pane' not in args:
            lm = ll.LayoutManager(window.layout())
            if args["direction"] == 'next':
                pos = lm.next(window.active_group())
            else:
                pos = lm.next(window.active_group(), -1)

            args["next_pane"] = pos
            return cmd, args

class SbpWindowCommand(sublime_plugin.WindowCommand):

    def run(self, **kwargs):
        self.util = CmdUtil(self.window.active_view(), state=ViewState.get(self.window.active_view()))
        self.run_cmd(self.util, **kwargs)

class SbpChainCommand(SbpTextCommand):
    """A command that easily runs a sequence of other commands."""

    def run_cmd(self, util, commands, use_window=False):
        for c in commands:
            if 'window_command' in c:
                util.run_window_command(c['window_command'], c['args'])
            elif 'command' in c:
                util.run_command(c['command'], c['args'])

#
# Calls run command a specified number of times.
#
class SbpDoTimesCommand(SbpTextCommand):
    def run_cmd(self, util, cmd, _times, **args):
        view = self.view
        window = view.window()
        visible = view.visible_region()
        def doit():
            for i in range(_times):
                window.run_command(cmd, args)

        if cmd in ('redo', 'undo'):
            sublime.set_timeout(doit, 10)
        else:
            doit()
            cursor = util.get_last_cursor()
            if not visible.contains(cursor.b):
                util.ensure_visible(cursor, True)

class SbpShowScopeCommand(SbpTextCommand):
    def run_cmd(self, util, direction=1):
        point = util.get_point()
        name = self.view.scope_name(point)
        region = self.view.extract_scope(point)
        status = "%d bytes: %s" % (region.size(), name)
        print(status)
        self.view.set_status(JOVE_STATUS, status)

#
# Advance to the beginning (or end if going backward) word unless already positioned at a word
# character. This can be used as setup for commands like upper/lower/capitalize words. This ignores
# the argument count.
#
class SbpMoveWordCommand(SbpTextCommand):
    should_reset_target_column = True
    is_ensure_visible_cmd = True

    def find_by_class_fallback(self, view, point, forward, classes, seperators):
      if forward:
        delta = 1
        end_position = self.view.size()
        if point > end_position:
          point = end_position
      else:
        delta = -1
        end_position = 0
        if point < end_position:
          point = end_position

      while point != end_position:
        if view.classify(point) & classes != 0:
          return point
        point += delta

      return point

    def find_by_class_native(self, view, point, forward, classes, separators):
        return view.find_by_class(point, forward, classes, separators)

    def run_cmd(self, util, direction=1):
        view = self.view

        separators = settings_helper.get("sbp_word_separators", default_sbp_word_separators)

        # determine the direction
        count = util.get_count() * direction
        forward = count > 0
        count = abs(count)

        def call_find_by_class(point, classes, separators):
          '''
          This is a small wrapper that maps to the right find_by_class call
          depending on the version of ST installed
          '''
          return self.find_by_class_native(view, point, forward, classes, separators)

        def move_word0(cursor, first=False):
            point = cursor.b
            if forward:
                if not first or not util.is_word_char(point, True, separators):
                    point = call_find_by_class(point, sublime.CLASS_WORD_START, separators)
                point = call_find_by_class(point, sublime.CLASS_WORD_END, separators)
            else:
                if not first or not util.is_word_char(point, False, separators):
                    point = call_find_by_class(point, sublime.CLASS_WORD_END, separators)
                point = call_find_by_class(point, sublime.CLASS_WORD_START, separators)

            return sublime.Region(point, point)

        for c in range(count):
            util.for_each_cursor(move_word0, first=(c == 0))

#
# Advance to the beginning (or end if going backward) word unless already positioned at a word
# character. This can be used as setup for commands like upper/lower/capitalize words. This ignores
# the argument count.
#
class SbpMoveBackToIndentation(SbpTextCommand):
    should_reset_target_column = True

    def run_cmd(self, util, direction=1):
        view = self.view

        def to_indentation(cursor):
            start = cursor.begin()
            while util.is_one_of(start, " \t"):
                start += 1
            return start

        util.run_command("move_to", {"to": "hardbol", "extend": False})
        util.for_each_cursor(to_indentation)

#
# Advance to the beginning (or end if going backward) word unless already positioned at a word
# character. This can be used as setup for commands like upper/lower/capitalize words. This ignores
# the argument count.
#
class SbpToWordCommand(SbpTextCommand):
    should_reset_target_column = True

    def run_cmd(self, util, direction=1):
        view = self.view

        separators = settings_helper.get("sbp_word_separators", default_sbp_word_separators)
        forward = direction > 0

        def to_word(cursor):
            point = cursor.b
            if forward:
                if not util.is_word_char(point, True, separators):
                    point = view.find_by_class(point, True, sublime.CLASS_WORD_START, separators)
            else:
                if not util.is_word_char(point, False, separators):
                    point = view.find_by_class(point, False, sublime.CLASS_WORD_END, separators)

            return sublime.Region(point, point)

        util.for_each_cursor(to_word)

#
# Change the case of the current region. Not sure this is ... multi-cursor/region aware.
#
class SbpCaseRegion(SbpTextCommand):

    def run_cmd(self, util, mode):
        region = util.get_regions()
        text = util.view.substr(region)
        if mode == "upper":
            text = text.upper()
        elif mode == "lower":
            text = text.lower()
        else:
            util.set_status("Unknown Mode")
            return

        util.view.replace(util.edit, region, text)


#
# Perform the uppercase/lowercase/capitalize commands on all the current cursors.
#
class SbpCaseWordCommand(SbpTextCommand):
    should_reset_target_column = True

    def run_cmd(self, util, mode, direction=1):
        # This works first by finding the bounds of the operation by executing a forward-word
        # command. Then it performs the case command.
        view = self.view
        count = util.get_count(True)

        # copy the cursors
        selection = view.sel()
        regions = list(selection)

        # If the regions are all empty, we just move from where we are to where we're going. If
        # there are regions, we use the regions and just do the cap, lower, upper within that
        # region. That's different from Emacs but I think this is better than emacs.
        empty = util.all_empty_regions(regions)

        if empty:
            # run the move-word command so we can create a region
            direction = -1 if count < 0 else 1
            util.run_command("sbp_move_word", {"direction": 1})

            # now the selection is at the "other end" and so we create regions out of all the
            # cursors
            new_regions = []
            for r, s in zip(regions, selection):
                new_regions.append(r.cover(s))
            selection.clear()
            selection.add_all(new_regions)

        # perform the operation
        if mode in ('upper', 'lower'):
            util.run_command(mode + "_case", {})
        else:
            for r in selection:
                util.view.replace(util.edit, r, view.substr(r).title())

        if empty:
            if count < 0:
                # restore cursors to original state if direction was backward
                selection.clear()
                selection.add_all(regions)
            else:
                # otherwise we leave the cursors at the end of the regions
                for r in new_regions:
                    r.a = r.b = r.end()
                selection.clear()
                selection.add_all(new_regions)

#
# A poor implementation of moving by s-expressions. The problem is it tries to use the built-in
# sublime capabilities for matching brackets, and it can be tricky getting that to work.
#
# The real solution is to figure out how to require/request the bracket highlighter code to be
# loaded and just use it.
#
class SbpMoveSexprCommand(SbpTextCommand):
    is_ensure_visible_cmd = True
    should_reset_target_column = True

    def run_cmd(self, util, direction=1):
        view = self.view


        separators = settings_helper.get("sbp_sexpr_separators", default_sbp_sexpr_separators)

        # determine the direction
        count = util.get_count() * direction
        forward = count > 0
        count = abs(count)

        def advance(cursor, first):
            point = cursor.b
            if forward:
                limit = view.size()
                while point < limit:
                    if util.is_word_char(point, True, separators):
                        point = view.find_by_class(point, True, sublime.CLASS_WORD_END, separators)
                        break
                    else:
                        ch = view.substr(point)
                        if ch in "({['\"":
                            next_point = util.to_other_end(point, direction)
                            if next_point is not None:
                                point = next_point
                                break
                        point += 1
            else:
                while point > 0:
                    if util.is_word_char(point, False, separators):
                        point = view.find_by_class(point, False, sublime.CLASS_WORD_START, separators)
                        break
                    else:
                        ch = view.substr(point - 1)
                        if ch in ")}]'\"":
                            next_point = util.to_other_end(point, direction)
                            if next_point is not None:
                                point = next_point
                                break
                        point -= 1

            cursor.a = cursor.b = point
            return cursor

        for c in range(count):
            util.for_each_cursor(advance, (c == 0))

# Move to paragraph depends on the functionality provided by the default
# plugin in ST. So for now we use this.
class SbpMoveToParagraphCommand(SbpTextCommand):

    def run_cmd(self, util, direction=1):
        # Clear all selections
        s = self.view.sel()[0]
        if direction == 1:
            if s.begin() == 0:
                return
            point = paragraph.expand_to_paragraph(self.view, s.begin()-1).begin()
        else:
            if s.end() == self.view.size():
                return
            point = paragraph.expand_to_paragraph(self.view, s.end()+1).end()

        self.view.sel().clear()
        #Clear selections

        if point < 0:
            point = 0

        if point > self.view.size():
            point = self.view.size()

        self.view.sel().add(sublime.Region(point, point))
        self.view.show(self.view.sel()[0].begin())

#
# A class which implements all the hard work of performing a move and then delete/kill command. It
# keeps track of the cursors, then runs the command to move all the cursors, and then performs the
# kill. This is used by the generic SbpMoveThenDeleteCommand command, but also commands that require
# input from a panel and so are not synchronous.
#
class MoveThenDeleteHelper():
    def __init__(self, util):
        self.util = util
        self.selection = util.view.sel()

        # assume forward kill direction
        self.forward = True

        # remember the current cursor positions
        self.orig_cursors = [s for s in self.selection]

    #
    # Finish the operation. Sometimes we're called later with a new util object, because the whole
    # thing was done asynchronously (see the zap code).
    #
    def finish(self, new_util=None):
        util = new_util if new_util else self.util
        view = util.view
        selection = self.selection
        orig_cursors = self.orig_cursors

        # extend each cursor so we can delete the bytes, and only if there is only one region will
        # we add the data to the kill ring
        new_cursors = [s for s in selection]

        selection.clear()
        for old,new in zip(orig_cursors, new_cursors):
            if old < new:
                selection.add(sublime.Region(old.begin(), new.end()))
            else:
                selection.add(sublime.Region(new.begin(), old.end()))

        # check to see if any regions will overlap each other after we perform the kill
        cursors = list(selection)
        regions_overlap = False
        for i, c in enumerate(cursors[1:]):
            if cursors[i].contains(c.begin()):
                regions_overlap = True
                break

        if regions_overlap:
            # restore everything to previous state
            selection.clear()
            selection.add_all(orig_cursors)
            return

        # copy the text into the kill ring
        regions = [view.substr(r) for r in view.sel()]
        kill_ring.add(regions, forward=self.forward, join=util.state.last_was_kill_cmd())

        # erase the regions
        for region in selection:
            view.erase(util.edit, region)



#
# This command remembers all the current cursor positions, executes a command on all the cursors,
# and then deletes all the data between the two.
#
# If there's only one selection, the deleted data is added to the kill ring appropriately.
#
class SbpMoveThenDeleteCommand(SbpTextCommand):
    is_ensure_visible_cmd = True
    is_kill_cmd = True

    def run_cmd(self, util, move_cmd, **kwargs):
        # prepare
        helper = MoveThenDeleteHelper(util)

        # peek at the count and update the helper's forward direction
        count = util.get_count(True)
        if 'direction' in kwargs:
            count *= kwargs['direction']
        helper.forward = count > 0

        util.view.run_command(move_cmd, kwargs)
        helper.finish()

#
# Goto the the Nth line as specified by the emacs arg count, or prompt for a line number of one
# isn't specified.
#
class SbpGotoLineCommand(SbpTextCommand):
    def run_cmd(self, util):
        if util.has_prefix_arg():
            util.goto_line(util.get_count())
        else:
            util.run_window_command("show_overlay", {"overlay": "goto", "text": ":"})

#
# Emacs delete-white-space command.
#
class SbpDeleteWhiteSpaceCommand(SbpTextCommand):
    def run_cmd(self, util):
        util.for_each_cursor(self.delete_white_space, util, can_modify=True)

    def delete_white_space(self, cursor, util, **kwargs):
        view = self.view
        line = view.line(cursor.a)
        data = view.substr(line)
        row,col = view.rowcol(cursor.a)
        start = col
        while start - 1 >= 0 and data[start-1: start] in (" \t"):
            start -= 1
        end = col
        limit = len(data)
        while end + 1 < limit and data[end:end+1] in (" \t"):
            end += 1
        view.erase(util.edit, sublime.Region(line.begin() + start, line.begin() + end))
        return None

class SbpUniversalArgumentCommand(SbpTextCommand):
    def run_cmd(self, util, value):
        state = util.state
        if not state.argument_supplied:
            state.argument_supplied = True
            if value == 'by_four':
                state.argument_value = 4
            elif value == 'negative':
                state.argument_negative = True
            else:
                state.argument_value = value
        elif value == 'by_four':
            state.argument_value *= 4
        elif isinstance(value, int):
            state.argument_value *= 10
            state.argument_value += value
        elif value == 'negative':
            state.argument_value = -state.argument_value

class SbpShiftRegionCommand(SbpTextCommand):
    """Shifts the emacs region left or right."""

    def run_cmd(self, util, direction):
        view = self.view
        state = util.state
        regions = util.get_regions()
        if not regions:
            regions = util.get_cursors()
        if regions:
            util.save_cursors("shift")
            util.toggle_active_mark_mode(False)
            selection = self.view.sel()
            selection.clear()

            # figure out how far we're moving
            if state.argument_supplied:
                cols = direction * util.get_count()
            else:
                cols = direction * util.get_tab_size()

            # now we know which way and how far we're shifting, create a cursor for each line we
            # want to shift
            amount = abs(cols)
            count = 0
            shifted = 0
            for region in regions:
                for line in util.for_each_line(region):
                    count += 1
                    if cols < 0 and (line.size() < amount or not util.is_blank(line.a, line.a + amount)):
                        continue
                    selection.add(sublime.Region(line.a, line.a))
                    shifted += 1

            # shift the region
            if cols > 0:
                # shift right
                self.view.run_command("insert", {"characters": " " * cols})
            else:
                for i in range(amount):
                    self.view.run_command("right_delete")

            # restore the region
            util.restore_cursors("shift")
            sublime.set_timeout(lambda: util.set_status("Shifted %d of %d lines in the region" % (shifted, count)), 100)

# Enum definition
def enum(**enums):
    return type('Enum', (), enums)

SCROLL_TYPES = enum(TOP=1, CENTER=0, BOTTOM=2)

class SbpCenterViewCommand(SbpTextCommand):
    '''
    Reposition the view so that the line containing the cursor is at the
    center of the viewport, if possible. Like the corresponding Emacs
    command, recenter-top-bottom, this command cycles through
    scrolling positions. If the prefix args are used it centers given an offset
    else the cycling command is used

    This command is frequently bound to Ctrl-l.
    '''

    last_sel = None
    last_scroll_type = None
    last_visible_region = None

    def rowdiff(self, start, end):
        r1,c1 = self.view.rowcol(start)
        r2,c2 = self.view.rowcol(end)
        return r2 - r1

    def run_cmd(self, util, center_only=False):
        view = self.view
        point = util.get_point()
        if util.has_prefix_arg():
            lines = util.get_count()
            line_height = view.line_height()
            ignore, point_offy = view.text_to_layout(point)
            offx, ignore = view.viewport_position()
            view.set_viewport_position((offx, point_offy - line_height * lines))
        elif center_only:
            self.view.show_at_center(util.get_point())
        else:
            self.cycle_center_view(view.sel()[0])

    def cycle_center_view(self, start):
        if start != SbpCenterViewCommand.last_sel:
            SbpCenterViewCommand.last_visible_region = None
            SbpCenterViewCommand.last_scroll_type = SCROLL_TYPES.CENTER
            SbpCenterViewCommand.last_sel = start
            self.view.show_at_center(SbpCenterViewCommand.last_sel)
            return
        else:
            SbpCenterViewCommand.last_scroll_type = (SbpCenterViewCommand.last_scroll_type + 1) % 3

        SbpCenterViewCommand.last_sel = start
        if SbpCenterViewCommand.last_visible_region == None:
            SbpCenterViewCommand.last_visible_region = self.view.visible_region()

        # Now Scroll to position
        if SbpCenterViewCommand.last_scroll_type == SCROLL_TYPES.CENTER:
            self.view.show_at_center(SbpCenterViewCommand.last_sel)
        elif SbpCenterViewCommand.last_scroll_type == SCROLL_TYPES.TOP:
            row,col = self.view.rowcol(SbpCenterViewCommand.last_visible_region.end())
            diff = self.rowdiff(SbpCenterViewCommand.last_visible_region.begin(), SbpCenterViewCommand.last_sel.begin())
            self.view.show(self.view.text_point(row + diff-2, 0), False)
        elif SbpCenterViewCommand.last_scroll_type == SCROLL_TYPES.BOTTOM:
            row, col = self.view.rowcol(SbpCenterViewCommand.last_visible_region.begin())
            diff = self.rowdiff(SbpCenterViewCommand.last_sel.begin(), SbpCenterViewCommand.last_visible_region.end())
            self.view.show(self.view.text_point(row - diff+2, 0), False)

class SbpSetMarkCommand(SbpTextCommand):
    def run_cmd(self, util):
        state = util.state
        if state.argument_supplied:
            cursors = state.mark_ring.pop()
            if cursors:
                util.set_cursors(cursors)
            else:
                util.set_status("No mark to pop!")
            state.this_cmd = "sbp_pop_mark"
        elif state.this_cmd == state.last_cmd:
            # at least two set mark commands in a row: turn ON the highlight
            util.toggle_active_mark_mode()
        else:
            # set the mark
            util.set_mark()

        if settings_helper.get("sbp_active_mark_mode", False):
            util.set_active_mark_mode()

class SbpCancelMarkCommand(SbpTextCommand):
    def run_cmd(self, util):
        if util.state.active_mark:
            util.toggle_active_mark_mode()
        util.state.mark_ring.clear()

class SbpSwapPointAndMarkCommand(SbpTextCommand):
    def run_cmd(self, util, toggle_active_mark_mode=False):
        if util.state.argument_supplied or toggle_active_mark_mode:
            util.toggle_active_mark_mode()
        else:
            util.swap_point_and_mark()

class SbpMoveToCommand(SbpTextCommand):
    is_ensure_visible_cmd = True
    def run_cmd(self, util, to, always_push_mark=False):
        if to == 'bof':
            util.push_mark_and_goto_position(0)
        elif to == 'eof':
            util.push_mark_and_goto_position(self.view.size())
        elif to in ('eow', 'bow'):
            visible = self.view.visible_region()
            pos = visible.a if to == 'bow' else visible.b
            if always_push_mark:
                util.push_mark_and_goto_position(pos)
            else:
                util.set_cursors([sublime.Region(pos)])

class SbpOpenLineCommand(SbpTextCommand):
    def run_cmd(self, util):
        view = self.view
        for point in view.sel():
            view.insert(util.edit, point.b, "\n")
        view.run_command("move", {"by": "characters", "forward": False})

class SbpKillRegionCommand(SbpTextCommand):
    is_kill_cmd = True
    def run_cmd(self, util, is_copy=False):
        view = self.view
        regions = util.get_regions()
        if regions:
            data = [view.substr(r) for r in regions]
            kill_ring.add(data, True, False)
            if not is_copy:
                for r in reversed(regions):
                    view.erase(util.edit, r)
            else:
                bytes = sum(len(d) for d in data)
                util.set_status("Copied %d bytes in %d regions" % (bytes, len(data)))
            util.toggle_active_mark_mode(False)

class SbpPaneCmdCommand(SbpWindowCommand):

    def run_cmd(self, util, cmd, **kwargs):
        if cmd == 'split':
            self.split(self.window, util, **kwargs)
        elif cmd == 'grow':
            self.grow(self.window, util, **kwargs)
        elif cmd == 'destroy':
            self.destroy(self.window, **kwargs)
        elif cmd in ('move', 'switch_tab'):
            self.move(self.window, **kwargs)
        else:
            print("Unknown command")

    #
    # Grow the current selected window group (pane). Amount is usually 1 or -1 for grow and shrink.
    #
    def grow(self, window, util, direction):
        if window.num_groups() == 1:
            return

        # Prepare the layout
        layout = window.layout()
        lm = ll.LayoutManager(layout)
        rows = lm.rows()
        cols = lm.cols()
        cells = layout['cells']

        # calculate the width and height in pixels of all the views
        width = height = dx = dy = 0

        for g,cell in enumerate(cells):
            view = window.active_view_in_group(g)
            w,h = view.viewport_extent()
            width += w
            height += h
            dx += cols[cell[2]] - cols[cell[0]]
            dy += rows[cell[3]] - rows[cell[1]]
        width /= dx
        height /= dy

        current = window.active_group()
        view = util.view

        # Handle vertical moves
        count = util.get_count()
        if direction in ('g', 's'):
            unit = view.line_height() / height
        else:
            unit = view.em_width() / width

        window.set_layout(lm.extend(current, direction, unit, count))

        # make sure point doesn't disappear in any active view - a delay is needed for this to work
        def ensure_visible():
            for g in range(window.num_groups()):
                view = window.active_view_in_group(g)
                util = CmdUtil(view)
                util.ensure_visible(util.get_last_cursor())
        sublime.set_timeout(ensure_visible, 50)

    #
    # Split the current pane in half. Clone the current view into the new pane. Refuses to split if
    # the resulting windows would be too small.
    def split(self, window, util, stype):
        layout = window.layout()
        current = window.active_group()
        group_count = window.num_groups()

        view = window.active_view()
        extent = view.viewport_extent()
        if stype == "h" and extent[1] / 2 <= 4 * view.line_height():
            return False

        if stype == "v" and extent[0] / 2 <= 20 * view.em_width():
            return False


        # Perform the layout
        lm = ll.LayoutManager(layout)
        if not lm.split(current, stype):
            return False

        window.set_layout(lm.build())

        # couldn't find an existing view so we have to clone the current one
        window.run_command("clone_file")

        # the cloned view becomes the new active view
        new_view = window.active_view()

        # move the new view into the new group (add the end of the list)
        window.set_view_index(new_view, group_count, 0)

        # make sure the original view is the focus in the original pane
        window.focus_view(view)

        # switch to new pane
        window.focus_group(group_count + 1)

        # after a short delay make sure the two views are looking at the same area
        def setup_views():
            selection = new_view.sel()
            selection.clear()
            selection.add_all([r for r in view.sel()])
            new_view.set_viewport_position(view.viewport_position(), False)

            point = util.get_point()
            new_view.show(point)
            view.show(point)

        sublime.set_timeout(setup_views, 10)
        return True

    #
    # Destroy the specified pane=self|others.
    #
    def destroy(self, window, pane):
        if window.num_groups() == 1:
            return
        view = window.active_view()
        layout = window.layout()

        current = window.active_group()
        lm = ll.LayoutManager(layout)

        if pane == "self":
            views = [window.active_view_in_group(i) for i in range(window.num_groups())]
            del(views[current])
            lm.killSelf(current)
        else:
            lm.killOther(current)
            views = [window.active_view()]

        window.set_layout(lm.build())


        for i in range(window.num_groups()):
            view = views[i]
            window.focus_group(i)
            window.focus_view(view)

        window.focus_group(max(0, current - 1))
        dedup_views(window)


    def move(self, window, **kwargs):
        if 'next_pane' in kwargs:
            window.focus_group(kwargs["next_pane"])
            return

        direction = kwargs['direction']
        if direction in ("prev", "next"):
            direction = 1 if direction == "next" else -1
            current = window.active_group()
            current += direction
            num_groups = window.num_groups()
            if current < 0:
                current = num_groups - 1
            elif current >= num_groups:
                current = 0
            window.focus_group(current)
        else:
            view = window.active_view()
            group,index = window.get_view_index(view)
            views = window.views_in_group(group)
            direction = 1 if direction == "right" else -1
            index += direction
            if index >= len(views):
                index = 0
            elif index < 0:
                index = len(views) - 1
            window.focus_view(views[index])

#
# Close the N least recently touched views, leaving at least one view remaining.
#
class SbpCloseOlderViewsCommand(SbpWindowCommand):
    def run_cmd(self, util, n_windows=10):
        window = sublime.active_window()
        sorted = ViewState.sorted_views(window)
        while n_windows > 0 and len(sorted) > 1:
            view = sorted.pop()
            if view.is_dirty():
                continue
            window.focus_view(view)
            window.run_command('close')
            n_windows -= 1

        # go back to the original view
        window.focus_view(util.view)

#
# Closes the current view and selects the most recently used one in its place. This is almost like
# kill buffer in emacs but if another view is displaying this file, it will still exist there. In
# short, this is like closing a tab but rather than selecting an adjacent tab, it selects the most
# recently used "buffer".
#
class SbpCloseCurrentViewCommand(SbpWindowCommand):
    def run_cmd(self, util, n_windows=10):
        window = sublime.active_window()
        sorted = ViewState.sorted_views(window)
        if len(sorted) > 0:
            view = sorted.pop(0)
            window.focus_view(view)
            window.run_command('close')
            if len(sorted) > 0:
                window.focus_view(sorted[0])
        else:
            window.run_command('close')

#
# Exists only to support kill-line with multiple cursors.
#
class SbpMoveForKillLineCommand(SbpTextCommand):
    def run_cmd(self, util, **kwargs):
        view = self.view
        state = util.state

        if state.argument_supplied:
            # we don't support negative arguments for kill-line
            count = abs(util.get_count())
            line_mode = True
        else:
            line_mode = False

        def advance(cursor):
            start = cursor.b
            text,index,region = util.get_line_info(start)

            if line_mode:
                # go down N lines
                for i in range(abs(count)):
                    view.run_command("move", {"by": "lines", "forward": True})

                end = util.get_point()
                if region.contains(end):
                    # same line we started on - must be on the last line of the file
                    end = region.end()
                else:
                    # beginning of the line we ended up on
                    end = view.line(util.get_point()).begin()
                    util.set_cursors(sublime.Region(end))
            else:
                end = region.end()

                # check if line is blank from here to the end and if so, delete the \n as well
                import re
                if re.match(r'[ \t]*$', text[index:]) and end < util.view.size():
                    end += 1

            return sublime.Region(end, end)

        util.for_each_cursor(advance)

class SbpYankCommand(SbpTextCommand):
    def run_cmd(self, util, pop=0):
        if pop and util.state.last_cmd != 'sbp_yank':
            util.set_status("Previous command was not yank!")
            return

        view = self.view

        # Get the cursors as selection, because if there is a selection we want to replace it with
        # what we're yanking.
        cursors = list(view.sel())
        data = kill_ring.get_current(len(cursors), pop)
        if not data:
            return
        if pop != 0:
            # erase existing regions
            regions = util.get_regions()
            if not regions:
                return
            for r in reversed(regions):
                view.erase(util.edit, r)

            # fetch updated cursors
            cursors = util.get_cursors()

        for region, data in reversed(list(zip(cursors, data))):
            view.replace(util.edit, region, data)
        util.state.mark_ring.set(util.get_cursors(begin=True), True)
        util.make_cursors_empty()
        util.ensure_visible(util.get_last_cursor())

class SbpChooseAndYank(SbpTextCommand):
    def run_cmd(self, util):
        # items is an array of (index, text) pairs
        items = kill_ring.get_popup_sample(util.view)

        def on_done(idx):
            if idx >= 0:
                kill_ring.set_current(items[idx][0])
                util.run_command("sbp_yank", {})

        if items:
            sublime.active_window().show_quick_panel([item[1] for item in items], on_done)
        else:
            sublime.status_message('Nothing in history')


class SbpIncSearchCommand(SbpTextCommand):
    def run_cmd(self, util, cmd=None, **kwargs):
        info = isearch_info_for(self.view)
        if info is None or cmd is None:
            regex = kwargs.get('regex', False)
            if util.state.argument_supplied:
                regex = not regex
            info = set_isearch_info_for(self.view, ISearchInfo(self.view, kwargs['forward'], regex))
            info.open()
        else:
            if cmd == "next":
                info.next(**kwargs)
            elif cmd == "pop":
                info.pop()
            elif cmd == "append_from_cursor":
                info.append_from_cursor()
            elif cmd == "keep_all":
                info.keep_all()
            elif cmd == "done":
                info.done()
            elif cmd == "quit":
                info.quit()
            elif cmd == "yank":
                info.input_view.run_command("sbp_yank")
            elif cmd == "set_search":
                view = info.input_view
                view.replace(util.edit, sublime.Region(0, view.size()), kwargs['text'])
                view.run_command("move_to", {"to": "eof"})
            elif cmd == "history":
                info.history(**kwargs)
            else:
                print("Not handling cmd", cmd, kwargs)

    def is_visible(self, **kwargs):
        # REMIND: is it not possible to invoke isearch from the menu for some reason. I think the
        # problem is that a focus thing is happening and we're dismissing ourselves as a result. So
        # for now we hide it.
        return False

class SbpIncSearchEscapeCommand(SbpTextCommand):
    # unregistered = True
    def run_cmd(self, util, next_cmd, next_args):
        info = isearch_info_for(self.view)
        info.done()
        info.view.run_command(next_cmd, next_args)

#
# Indent for tab command. If the cursor is not within the existing indent, just call reindent. If
# the cursor is within the indent, move to the start of the indent and call reindent. If the cursor
# was already at the indent didn't change after calling reindent, indent one more level.
#
class SbpTabCmdCommand(SbpTextCommand):
    def run_cmd(self, util, indent_on_repeat=False):
        point = util.get_point()
        indent,cursor = util.get_line_indent(point)
        tab_size = util.get_tab_size()
        if util.state.active_mark or cursor > indent:
            util.run_command("reindent", {})
        else:
            if indent_on_repeat and util.state.last_cmd == util.state.this_cmd:
                util.run_command("indent", {})
            else:
                # sublime gets screwy with indent if you're not currently a multiple of tab size
                if (indent % tab_size) != 0:
                    delta = tab_size - (indent % tab_size)
                    self.view.run_command("insert", {"characters": " " * delta})
                if cursor < indent:
                    util.run_command("move_to", {"to": "bol", "extend": False})
                util.run_command("reindent", {})

class SbpQuitCommand(SbpTextCommand):
    def run_cmd(self, util):
        window = self.view.window()

        info = isearch_info_for(self.view)
        if info:
            info.quit()
            return

        for cmd in ['clear_fields', 'hide_overlay', 'hide_auto_complete', 'hide_panel']:
            window.run_command(cmd)

        if util.state.active_mark:
            util.toggle_active_mark_mode()
            return

        # If there is a selection, set point to the end of it that is visible.
        s = list(self.view.sel())
        if s:
            start = s[0].a
            end = s[-1].b

            if util.is_visible(end):
                pos = end
            elif util.is_visible(start):
                pos = start
            else:
                # set point to the beginning of the line in the middle of the window
                visible = self.view.visible_region()
                top_line = self.view.rowcol(visible.begin())[0]
                bottom_line = self.view.rowcol(visible.end())[0]
                pos = self.view.text_point((top_line + bottom_line) / 2, 0)
            util.set_selection(sublime.Region(pos))

#
# A class which knows how to ask for a single character and then does something with it.
#
class AskCharOrStringBase(SbpTextCommand):
    def run_cmd(self, util, prompt="Type character"):
        self.util = util
        self.window = self.view.window()
        self.count = util.get_count()
        self.mode = "char"

        # kick things off by showing the panel
        self.window.show_input_panel(prompt, "", self.on_done, self.on_change, None)

    def on_change(self, content):
        # on_change is notified immediate upon showing the panel before a key is even pressed
        if self.mode == "word" or len(content) < 1:
            return
        self.process_cursors(content)

    def process_cursors(self, content):
        util = self.util
        self.window.run_command("hide_panel")

        count = abs(self.count)
        for i in range(count):
            self.last_iteration = (i == count - 1)
            util.for_each_cursor(self.process_one, content)

    def on_done(self, content):
        if self.mode == "word":
            self.process_cursors(content)

#
# Jump to char command inputs one character and jumps to it. If plus_one is True it goes just past
# the character in question, otherwise it stops just before it.
#
class SbpJumpToCharCommand(AskCharOrStringBase):
    def run_cmd(self, util, *args, plus_one=False, **kwargs):
        if 'prompt' not in kwargs:
            kwargs['prompt'] = "Jump to char: "
        super(SbpJumpToCharCommand, self).run_cmd(util, *args, **kwargs)
        self.plus_one = plus_one

    def process_one(self, cursor, ch):
        r = self.view.find(ch, cursor.end(), sublime.LITERAL)
        if r:
            p = r.begin()
            if self.plus_one or not self.last_iteration:
                # advance one more if this is not the last_iteration or else we'll forever be stuck
                # at the same position
                p += 1
            return p
        return None

class SbpZapToCharCommand(SbpJumpToCharCommand):
    is_kill_cmd = True
    def run_cmd(self, util, **kwargs):
        # prepare
        self.helper = MoveThenDeleteHelper(util)
        kwargs['prompt'] = "Zap to char: "
        super(SbpZapToCharCommand, self).run_cmd(util, **kwargs)

    def process_cursors(self, content):
        # process cursors does all the work (of jumping) and then ...
        super(SbpZapToCharCommand, self).process_cursors(content)

        # Save the helper in view state and invoke a command to make use of it. We can't use it now
        # because we don't have access to a valid edit object, because this function
        # (process_cursors) is called asynchronously after the original text command has returned.
        vs = ViewState.get(self.view)
        vs.pending_move_then_delete_helper = self.helper

        # ... we can finish what we started
        self.window.run_command("sbp_finish_move_then_delete")

#
# A helper class which will simply finish what was started in a previous command that was using a
# MoveThenDeleteHelper class. Some commands return before they are finished (e.g., they pop up a
# panel) and so we need a new 'edit' instance to be able to perform any edit operations. This is how
# we do that.
#
class SbpFinishMoveThenDeleteCommand(SbpTextCommand):
    is_kill_cmd = True
    def run_cmd(self, util):
        vs = ViewState.get(self.view)
        helper = vs.pending_move_then_delete_helper
        vs.pending_move_then_delete_helper = None
        helper.finish(util)

#
# Jump to char command inputs one character and jumps to it. If plus_one is True it goes just past
# the character in question, otherwise it stops just before it.
#
class SbpJumpToWordCommand(AskCharOrStringBase):
    def run_cmd(self, util, *args, **kwargs):
        super(SbpJumpToWordCommand, self).run_cmd(util, *args, prompt="Jump to word: ", **kwargs)
        self.mode = "word"

    def process(self, cursor, word):
        r = self.view.find(word, cursor.end(), sublime.LITERAL)
        if r:
            p = r.end()
            return p
        return None

class SbpConvertPlistToJsonCommand(SbpTextCommand):
    JSON_SYNTAX = "Packages/Javascript/JSON.tmLanguage"
    PLIST_SYNTAX = "Packages/XML/XML.tmLanguage"

    def run_cmd(self, util):
        import json
        from plistlib import readPlistFromBytes, writePlistToBytes

        data = self.view.substr(sublime.Region(0, self.view.size())).encode("utf-8")
        self.view.replace(util.edit, sublime.Region(0, self.view.size()),
                          json.dumps(readPlistFromBytes(data), indent=4, separators=(',', ': ')))
        self.view.set_syntax_file(JSON_SYNTAX)

class SbpConvertJsonToPlistCommand(SbpTextCommand):
    JSON_SYNTAX = "Packages/Javascript/JSON.tmLanguage"
    PLIST_SYNTAX = "Packages/XML/XML.tmLanguage"

    def run_cmd(self, util):
        import json
        from plistlib import readPlistFromBytes, writePlistToBytes

        data = json.loads(self.view.substr(sublime.Region(0, self.view.size())))
        self.view.replace(util.edit, sublime.Region(0, self.view.size()), writePlistToBytes(data).decode("utf-8"))
        self.view.set_syntax_file(PLIST_SYNTAX)

class SbpTrimTrailingWhiteSpaceAndEnsureNewlineAtEofCommand(sublime_plugin.TextCommand):
    def run(self, edit, trim_whitespace, ensure_newline):
        # make sure you trim trailing whitespace FIRST and THEN check for Newline
        if trim_whitespace:
            trailing_white_space = self.view.find_all("[\t ]+$")
            trailing_white_space.reverse()
            for r in trailing_white_space:
                self.view.erase(edit, r)
        if ensure_newline:
            if self.view.size() > 0 and self.view.substr(self.view.size() - 1) != '\n':
                self.view.insert(edit, self.view.size(), "\n")

class SbpPreSaveWhiteSpaceHook(sublime_plugin.EventListener):
    def on_pre_save(self, view):
        trim = settings_helper.get("sbp_trim_trailing_white_space_on_save") == True
        ensure = settings_helper.get("sbp_ensure_newline_at_eof_on_save") == True
        if trim or ensure:
            view.run_command("sbp_trim_trailing_white_space_and_ensure_newline_at_eof",
                             {"trim_whitespace": trim, "ensure_newline": ensure})


#
# Switch buffer command. "C-x b" equiv in emacs. This limits the set of files in a chooser to the
# ones currently loaded. We sort the files by last access hopefully like emacs.
#
class SbpSwitchToViewCommand(SbpTextCommand):
    def run(self, util):
        self.window = sublime.active_window()
        self.views = ViewState.sorted_views(self.window)
        self.roots = get_project_roots()
        self.original_view = self.window.active_view()
        self.highlight_count = 0

        # swap the top two views to enable switching back and forth like emacs
        if len(self.views) >= 2:
            # self.views[0], self.views[1] = self.views[1], self.views[0]
            index = 1
        else:
            index = 0
        self.window.show_quick_panel(self.get_items(), self.on_select, 0, index, self.on_highlight)

    def on_select(self, index):
        if index >= 0:
            self.window.focus_view(self.views[index])
        else:
            self.window.focus_view(self.original_view)

    def on_highlight(self, index):
        self.highlight_count += 1
        if self.highlight_count > 1:
            self.window.focus_view(self.views[index])

    def get_items(self):
        return [[self.get_display_name(view), self.get_path(view)] for view in self.views]

    def get_display_name(self, view):
        mod_star = '*' if view.is_dirty() else ''

        if view.is_scratch() or not view.file_name():
            disp_name = view.name() if len(view.name()) > 0 else 'untitled'
        else:
            disp_name = os.path.basename(view.file_name())

        return '%s%s' % (disp_name, mod_star)

    def get_path(self, view):
        if view.is_scratch():
            return ''

        if not view.file_name():
            return '<unsaved>'

        return get_relative_path(self.roots, view.file_name())


#
# Function to dedup views in all the groups of the specified window. This does not close views that
# have changes because that causes a warning to popup. So we have a monitor which dedups views
# whenever a file is saved in order to dedup them then when it's safe.
#
def dedup_views(window):
    group = window.active_group()
    for g in range(window.num_groups()):
        found = dict()
        views = window.views_in_group(g)
        active = window.active_view_in_group(g)
        for v in views:
            if v.is_dirty():
                # we cannot nuke a dirty buffer or we'll get an annoying popup
                continue
            id = v.buffer_id()
            if id in found:
                if v == active:
                    # oops - nuke the one that's already been seen and put this one in instead
                    before = found[id]
                    found[id] = v
                    v = before
                window.focus_view(v)
                window.run_command('close')
            else:
                 found[id] = v
        window.focus_view(active)
    window.focus_group(group)

def plugin_loaded():
    # preprocess this module
    preprocess_module(sys.modules[__name__])
