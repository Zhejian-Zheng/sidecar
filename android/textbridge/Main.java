package dev.sidecar.textbridge;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.graphics.Rect;
import android.os.Bundle;
import android.os.HandlerThread;
import android.os.Looper;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONObject;

/** Single request, shell-only bridge. Never injects keys, clicks, focus or clipboard data. */
public final class Main {
    private static String string(CharSequence value) { return value == null ? "" : value.toString(); }

    private static void verify(JSONObject request) throws Exception {
        JSONObject binding = request.getJSONObject("display");
        int id = binding.getInt("id");
        if (id <= 0) throw new IllegalArgumentException();
        String boot = new String(Files.readAllBytes(Paths.get("/proc/sys/kernel/random/boot_id"))).trim();
        if (!boot.equals(binding.getString("boot_id"))) throw new IllegalStateException();
        Class<?> manager = Class.forName("android.hardware.display.DisplayManagerGlobal");
        Object instance = manager.getMethod("getInstance").invoke(null);
        Display display = (Display) manager.getMethod("getRealDisplay", int.class).invoke(instance, id);
        if (display == null || !display.isValid()) throw new IllegalStateException();
        String unique = (String) Display.class.getMethod("getUniqueId").invoke(display);
        if (!unique.equals(binding.getString("unique_id"))) throw new IllegalStateException();
    }

    private static boolean matches(AccessibilityNodeInfo node, JSONObject selector, String pkg) throws Exception {
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        JSONArray box = selector.getJSONArray("bounds");
        return node.isEditable() && node.isEnabled() && node.isVisibleToUser() && !node.isPassword()
            && pkg.equals(string(node.getPackageName()))
            && selector.getString("resource_id").equals(string(node.getViewIdResourceName()))
            && selector.getString("class_name").equals(string(node.getClassName()))
            && selector.getString("old_text").equals(string(node.getText()))
            && selector.getString("description").equals(string(node.getContentDescription()))
            && box.length() == 4 && bounds.equals(new Rect(box.getInt(0), box.getInt(1), box.getInt(2), box.getInt(3)))
            && node.getActionList().contains(AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_TEXT);
    }

    private static void collect(AccessibilityNodeInfo node, JSONObject selector, String pkg,
                                List<AccessibilityNodeInfo> matches, int depth) throws Exception {
        if (node == null) return;
        if (depth > 100) throw new IllegalStateException();
        if (matches(node, selector, pkg)) matches.add(node);
        for (int i = 0; i < node.getChildCount(); i++) collect(node.getChild(i), selector, pkg, matches, depth + 1);
    }

    private static JSONObject write(UiAutomation automation, JSONObject request) throws Exception {
        verify(request);
        int id = request.getJSONObject("display").getInt("id");
        JSONObject selector = request.getJSONObject("selector");
        String pkg = request.getString("package");
        String text = request.getString("text");
        if (text.isEmpty() || text.length() > 4000) throw new IllegalArgumentException();
        List<AccessibilityWindowInfo> windows = automation.getWindowsOnAllDisplays().get(id);
        if (windows == null || windows.isEmpty()) throw new IllegalStateException();
        List<AccessibilityNodeInfo> candidates = new ArrayList<>();
        for (AccessibilityWindowInfo window : windows) {
            if (window.getDisplayId() != id) throw new IllegalStateException();
            collect(window.getRoot(), selector, pkg, candidates, 0);
        }
        if (candidates.size() != 1) throw new IllegalStateException();
        AccessibilityNodeInfo target = candidates.get(0);
        verify(request);
        if (!target.refresh() || !matches(target, selector, pkg)) throw new IllegalStateException();
        AccessibilityWindowInfo targetWindow = target.getWindow();
        if (targetWindow == null || targetWindow.getDisplayId() != id) throw new IllegalStateException();
        Bundle arguments = new Bundle();
        arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        boolean ok = target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments);
        // Do not replay writes. A rejected or unverified result is an uncertain operation.
        boolean verified = ok && target.refresh() && text.equals(string(target.getText()));
        verify(request);
        return new JSONObject().put("ok", ok).put("verified", verified);
    }

    public static void main(String[] args) {
        Looper.prepareMainLooper();
        Thread watchdog = new Thread(() -> {
            try { Thread.sleep(10000); } catch (InterruptedException ignored) { return; }
            System.exit(2);
        });
        watchdog.setDaemon(true);
        watchdog.start();
        HandlerThread thread = new HandlerThread("sidecar-text");
        UiAutomation automation = null;
        int status = 1;
        try {
            String line = new BufferedReader(new InputStreamReader(System.in, "UTF-8")).readLine();
            if (line == null || line.length() > 65536) throw new IllegalArgumentException();
            JSONObject request = new JSONObject(line);
            verify(request);
            thread.start();
            Class<?> connection = Class.forName("android.app.IUiAutomationConnection");
            Object binder = Class.forName("android.app.UiAutomationConnection").getConstructor().newInstance();
            automation = UiAutomation.class.getConstructor(Looper.class, connection).newInstance(thread.getLooper(), binder);
            UiAutomation.class.getMethod("connect", int.class).invoke(automation, UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            AccessibilityServiceInfo info = automation.getServiceInfo();
            info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
            automation.setServiceInfo(info);
            Thread.sleep(150);
            JSONObject result = write(automation, request);
            System.out.println(result.toString());
            status = result.getBoolean("verified") ? 0 : 1;
        } catch (Exception failure) {
            System.out.println("{\"ok\":false,\"verified\":false}");
            System.err.println("Scoped text input rejected: " + failure.getClass().getSimpleName());
        } finally {
            if (automation != null) try { UiAutomation.class.getMethod("disconnect").invoke(automation); } catch (Exception ignored) { }
            thread.quitSafely();
        }
        System.exit(status);
    }
}
