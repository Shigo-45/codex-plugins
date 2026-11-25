/**
 * SPDX-FileCopyrightText: Copyright contributors to the CopilotJ project.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

package copilotj;

import org.scijava.Context;
import org.scijava.Priority;
import org.scijava.event.ContextDisposingEvent;
import org.scijava.event.EventService;
import org.scijava.log.LogService;
import org.scijava.plugin.Parameter;
import org.scijava.plugin.Plugin;
import org.scijava.service.AbstractService;
import org.scijava.service.Service;
import org.scijava.service.SciJavaService;

import net.imagej.ImageJ;
import net.imagej.patcher.LegacyInjector;
import net.imagej.legacy.LegacyService;

/**
 * Fiji service for the MCP-only distribution.
 *
 * <p>The service owns the native ImageJ event handler and starts the embedded
 * Streamable HTTP MCP endpoint during Fiji initialization. It contains no
 * model client, WebSocket bridge, managed Python environment, or web frontend.
 */
@Plugin(type = Service.class, priority = Priority.LOW)
public class McpOnlyService extends AbstractService implements SciJavaService {

  static {
    LegacyInjector.preinit();
  }

  /** Launches a development Fiji instance with the MCP-only service enabled. */
  public static void main(final String... args) throws Exception {
    final ImageJ ij = new ImageJ();
    ij.launch(args);
  }

  @Parameter
  private Context context;

  @Parameter
  private EventService eventService;

  @Parameter
  private LogService log;

  @Parameter
  private LegacyService legacy;

  private EventHandler eventHandler;
  private McpLoader loader;

  @Override
  public void initialize() {
    final boolean debug = Boolean.getBoolean("ij.debug");
    eventHandler = new EventHandler(context, log, debug);
    loader = new McpLoader(log);
    eventService.subscribe(this);

    if (!Boolean.parseBoolean(System.getProperty("copilotj.mcp.autostart", "true"))) {
      log.info("Image Analysis MCP autostart disabled");
      return;
    }

    final String host = System.getProperty("copilotj.mcp.host", "127.0.0.1");
    final int port = Integer.getInteger("copilotj.mcp.port", 3001);
    try {
      loader.startMcp(eventHandler, host, port);
      log.info("Image Analysis MCP started on " + host + ":" + port);
    } catch (final Exception e) {
      log.error("Image Analysis MCP failed to start: " + e.getMessage(), e);
    }
  }

  public EventHandler getEventHandler() {
    return eventHandler;
  }

  public boolean isMcpRunning() {
    return loader != null && loader.isMcpRunning();
  }

  @org.scijava.event.EventHandler
  private void onContextDisposing(final ContextDisposingEvent event) {
    if (loader != null) loader.stopMcp();
  }
}
