package io.github.xenix13.noxa.tv;

import android.content.Context;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;

import androidx.recyclerview.widget.RecyclerView;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Map;

/**
 * Remote navigation by rows, as in TV apps: left / right stay in the row,
 * up / down move to the neighbouring row and find the element that was left there. Android's "closest"
 * search, on the other hand, jumped from one row to another at the end of a line and rarely came back to the same place.
 */
final class Nav {
    private Nav() {}

    /** Choice of the landing element in a block (e.g. the open season rather than the first). */
    interface Entry {
        View get();
    }

    /** Horizontal row of cards: it is not left through the sides, and it remembers the last chosen card. */
    static final class Row extends RecyclerView {
        private int last;

        Row(Context c) {
            super(c);
        }

        @Override
        public void requestChildFocus(View child, View focused) {
            super.requestChildFocus(child, focused);
            int p = getChildAdapterPosition(child);
            if (p != NO_POSITION) last = p;
        }

        @Override
        public View focusSearch(View focused, int direction) {
            View result = super.focusSearch(focused, direction);
            if ((direction == FOCUS_LEFT || direction == FOCUS_RIGHT) && !BrowseScreen.inside(result, this)) {
                return focused;  // end of the row: stay on it
            }
            return result;
        }

        /** Card to select when arriving on the row: the one that was left there. */
        View entry() {
            Adapter<?> a = getAdapter();
            if (a == null || a.getItemCount() == 0) return null;
            int p = Math.min(last, a.getItemCount() - 1);
            ViewHolder vh = findViewHolderForAdapterPosition(p);
            if (vh != null) return vh.itemView;
            scrollToPosition(p);
            return getChildCount() > 0 ? getChildAt(0) : null;
        }
    }

    /** Column of blocks (rows, buttons, episode list…) walked block by block with up / down. */
    static final class Column extends LinearLayout {
        private final Map<View, Entry> entries = new HashMap<>();
        private final Map<View, View> remembered = new HashMap<>();

        Column(Context c) {
            super(c);
            setOrientation(VERTICAL);
        }

        /** Block whose landing element is chosen by the code (otherwise: the last selected, or the first). */
        void entry(View block, Entry e) {
            entries.put(block, e);
        }

        @Override
        public void removeAllViews() {
            super.removeAllViews();
            entries.clear();
            remembered.clear();
        }

        @Override
        public void requestChildFocus(View child, View focused) {
            super.requestChildFocus(child, focused);
            remembered.put(child, focused);
        }

        private int blockOf(View v) {
            for (int i = 0; i < getChildCount(); i++) if (BrowseScreen.inside(v, getChildAt(i))) return i;
            return -1;
        }

        /** Element to land on in a block, or null when it has nothing selectable. */
        View entryOf(View block) {
            if (block.getVisibility() != VISIBLE) return null;
            Entry e = entries.get(block);
            if (e != null) {
                View v = e.get();
                if (v != null && v.isShown()) return v;
            }
            Row row = findRow(block);
            if (row != null) return row.entry();
            View last = remembered.get(block);
            if (last != null && last.isShown() && BrowseScreen.inside(last, block)) return last;
            ArrayList<View> all = new ArrayList<>();
            block.addFocusables(all, FOCUS_DOWN);
            for (View v : all) if (v.isShown()) return v;
            return null;
        }

        private static Row findRow(View v) {
            if (v instanceof Row) return (Row) v;
            if (v instanceof ViewGroup) {
                ViewGroup g = (ViewGroup) v;
                for (int i = 0; i < g.getChildCount(); i++) {
                    Row r = findRow(g.getChildAt(i));
                    if (r != null) return r;
                }
            }
            return null;
        }

        @Override
        public View focusSearch(View focused, int direction) {
            View result = super.focusSearch(focused, direction);
            if (direction != FOCUS_UP && direction != FOCUS_DOWN) return result;
            int i = blockOf(focused);
            if (i < 0) return result;
            // in a block that is itself a vertical list (episodes): stay in it as long as possible
            if (result != null && BrowseScreen.inside(result, getChildAt(i)) && findRow(getChildAt(i)) == null) return result;
            int step = direction == FOCUS_DOWN ? 1 : -1;
            for (int j = i + step; j >= 0 && j < getChildCount(); j += step) {
                View e = entryOf(getChildAt(j));
                if (e != null) return e;
            }
            // nothing more below: stay; nothing more above: the top bar (see BrowseScreen)
            return direction == FOCUS_DOWN ? focused : result;
        }
    }
}
