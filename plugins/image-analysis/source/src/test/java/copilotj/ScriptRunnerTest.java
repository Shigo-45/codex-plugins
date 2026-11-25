/**
 * SPDX-FileCopyrightText: Copyright contributors to the CopilotJ project.
 * SPDX-License-Identifier: Apache-2.0
 */
package copilotj;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import java.util.Collections;
import java.io.Writer;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import org.junit.jupiter.api.Test;
import org.scijava.Context;
import org.scijava.log.LogService;
import org.scijava.module.ModuleService;
import org.scijava.script.ScriptInfo;
import org.scijava.script.ScriptModule;
import org.scijava.script.ScriptService;

class ScriptRunnerTest {
  @Test
  void timedOutSciJavaFutureBlocksRetryUntilTheEngineFinishes() throws Exception {
    Context context = mock(Context.class);
    ScriptService scripts = mock(ScriptService.class);
    when(context.service(ScriptService.class)).thenReturn(scripts);
    ModuleService modules = mock(ModuleService.class);
    when(context.service(ModuleService.class)).thenReturn(modules);
    ScriptModule module = mock(ScriptModule.class);
    when(modules.createModule(any(ScriptInfo.class))).thenReturn(module);
    CompletableFuture<ScriptModule> engine = new CompletableFuture<>();
    CountDownLatch started = new CountDownLatch(1);
    when(modules.run(eq(module), eq(true), any(Object[].class))).thenAnswer(call -> {
      started.countDown();
      return engine;
    });
    when(module.getOutputs()).thenReturn(Collections.<String, Object>singletonMap("status", "finished"));
    ScriptRunner runner = new ScriptRunner(context, mock(LogService.class));
    ScriptRunner.ScriptRequest request = new ScriptRunner.ScriptRequest();
    request.language = "jython";
    request.script = "pass";
    request.timeout = 1;
    try {
      ScriptRunner.Result timeout = runner.runScript(request);
      assertTrue(started.await(1, TimeUnit.SECONDS));
      assertTrue(timeout.err.contains("timed out"), timeout.err);
      assertFalse(engine.isDone());
      ScriptRunner.Result retry = runner.runScript(request);
      assertTrue(retry.err.contains("still running"), retry.err);
      verify(modules, times(1)).run(eq(module), eq(true), any(Object[].class));
    } finally {
      engine.complete(module);
    }
    // The retained waiting worker may still be returning from Future.get().
    long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3);
    ScriptRunner.Result result;
    do {
      Thread.sleep(10);
      result = runner.runScript(request);
    } while (result.err != null && result.err.contains("still running") && System.nanoTime() < deadline);
    assertNull(result.err, result.err);
    assertEquals("finished", result.outputs.get("status"));
    verify(modules, times(2)).run(eq(module), eq(true), any(Object[].class));
  }

  @Test
  void capturesEngineErrorsWithWriterInstalledBeforeSubmission() throws Exception {
    Context context = mock(Context.class);
    when(context.service(ScriptService.class)).thenReturn(mock(ScriptService.class));
    ModuleService modules = mock(ModuleService.class);
    when(context.service(ModuleService.class)).thenReturn(modules);
    ScriptModule module = mock(ScriptModule.class);
    when(modules.createModule(any(ScriptInfo.class))).thenReturn(module);
    final Writer[] writer = new Writer[1];
    doAnswer(call -> { writer[0] = call.getArgument(0); return null; })
        .when(module).setErrorWriter(any(Writer.class));
    when(modules.run(eq(module), eq(true), any(Object[].class))).thenAnswer(call -> {
      assertNotNull(writer[0], "Error writer must be installed before evaluation");
      writer[0].write("RuntimeError: intentional-mcp-test-error");
      return CompletableFuture.completedFuture(module);
    });
    ScriptRunner runner = new ScriptRunner(context, mock(LogService.class));
    ScriptRunner.ScriptRequest request = new ScriptRunner.ScriptRequest();
    request.language = "jython";
    request.script = "raise RuntimeError('intentional-mcp-test-error')";
    request.timeout = 5;
    ScriptRunner.Result result = runner.runScript(request);
    assertEquals("RuntimeError: intentional-mcp-test-error", result.err);
    assertNull(result.outputs);
  }
}
