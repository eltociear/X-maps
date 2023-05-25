# TODO perf reduce delta_t as much as possible to reduce latency

from metavision_sdk_core import PolarityFilterAlgorithm
from metavision_sdk_cv import ActivityNoiseFilterAlgorithm
from metavision_sdk_ui import EventLoop, BaseWindow, MTWindow, UIAction, UIKeyEvent

import numpy as np

from trigger_finder import RobustTriggerFinder
from bias_events_iterator import BiasEventsIterator, NonBufferedBiasEventsIterator
from stats_printer import StatsPrinter, SingleTimer
from cam_proj_calibration import CamProjCalibration
from x_maps_disparity import XMapsDisparity
from proj_time_map import ProjectorTimeMap
from disp_to_depth import DisparityToDepth

import click
import sys
import time
from dataclasses import dataclass



def generate_frame(evs, frame):
    frame[:, :] = 0
    frame[evs["y"], evs["x"]] = 255

@dataclass
class DepthReprojectionPipe:
    
    camera_width: int
    camera_height: int
    
    projector_width: int
    projector_height: int
    
    x_maps_disp: XMapsDisparity = None
    disp_to_depth: DisparityToDepth = None
    stats_printer: StatsPrinter = StatsPrinter()
    
    def setup(self, cli_params):
        with SingleTimer("Setting up calibration"):
            calib_obj = CamProjCalibration(cli_params["calib"], self.camera_width, self.camera_height, self.projector_width, self.projector_height)

        with SingleTimer("Setting up projector time map"):
            proj_time_map = ProjectorTimeMap(calib_obj, cli_params["projector_time_map"])

        with SingleTimer("Setting up projector X-map"):
            self.x_maps_disp = XMapsDisparity(calib_obj, proj_time_map, self.projector_width)

        with SingleTimer("Setting up disparity to depth"):
            self.disp_to_depth = DisparityToDepth(self.stats_printer, calib_obj, cli_params["z_near"], cli_params["z_far"])


@click.command()
@click.option("--projector-width", default=720, help="Projector width in pixels", type=int)
@click.option("--projector-height", default=1280, help="Projector height in pixels", type=int)
@click.option("--projector-fps", default=60, help="Projector fps", type=int)
@click.option(
    "--projector-time-map",
    help="Path to calibrated projector time map file (*.npy). If left empty, a linear time map will be used.",
    type=click.Path(),
)
@click.option("--z-near", default=0.1, help="Minimum depth [m] for visualization", type=float)
@click.option("--z-far", default=1.0, help="Maximum depth [m] for visualization", type=float)
@click.option(
    "--calib",
    help="path to yaml file with camera and projector intrinsic and extrinsic calibration",
    type=click.Path(),
    required=True,
)
@click.option("--bias", help="Path to bias file, only required for live camera", type=click.Path())
@click.option("--input", help="Either a .raw, .dat file for prerecordings. Don't specify for live capture.", type=click.Path())
@click.option("--no-frame-dropping", help="Process all events, even when processing lags behind the event stream", is_flag=True)
def main(projector_width, projector_height, projector_fps, **cli_params):
    print("Code sample showing how to create a simple application testing different noise filtering strategies.")
    print(
        "Available keyboard options:\n"
        "  - A: Filter events using the activity noise filter algorithm\n"
        "  - T: Filter events using the trail filter algorithm\n"
        "  - S: Filter events using the spatio temporal contrast algorithm\n"
        "  - E: Show all events\n"
        "  - Q/Escape: Quit the application\n"
    )

    # TODO remove these static values, retrieve from event stream
    camera_width = 640
    camera_height = 480

    pos_filter = PolarityFilterAlgorithm(1)

    # TODO revisit: does this have an effect on latency?
    activity_time_ths = int(1e6 / projector_fps)
    act_filter = ActivityNoiseFilterAlgorithm(camera_width, camera_height, activity_time_ths)

    pos_events_buf = PolarityFilterAlgorithm.get_empty_output_buffer()
    act_events_buf = ActivityNoiseFilterAlgorithm.get_empty_output_buffer()

    frame = np.zeros((camera_height, camera_width, 3), dtype=np.uint8)

    last_frame_produced_time = -1

    should_drop_frames = not cli_params["no_frame_dropping"]

    pipe = DepthReprojectionPipe(camera_width, camera_height, projector_width, projector_height)
    pipe.setup(cli_params)

    # Window - Graphical User Interface (Display filtered events and process keyboard events)
    with MTWindow(
        title="X Maps Depth", width=projector_width, height=projector_height, mode=BaseWindow.RenderMode.BGR
    ) as window:
        
        pipe.stats_printer.reset()

        def on_frame_evs(evs):
            """Callback from the trigger finder, evs contain the events of the current frame"""
            # generate_frame(evs, frame)
            # window.show_async(frame)

            nonlocal last_frame_produced_time

            with pipe.stats_printer.measure_time("x-maps disp"):
                point_cloud, disp_map = pipe.x_maps_disp.compute_event_disparity(evs)

            with pipe.stats_printer.measure_time("disp2depth"):
                depth_map = pipe.disp_to_depth.compute_depth_map(disp_map)

            window.show_async(depth_map)
            pipe.stats_printer.count("frames shown")

            last_frame_produced_time = time.perf_counter()

        trigger_finder = RobustTriggerFinder(projector_fps=projector_fps, stats=pipe.stats_printer, callback=on_frame_evs)

        def keyboard_cb(key, scancode, action, mods):
            if action != UIAction.RELEASE:
                return
            if key == UIKeyEvent.KEY_ESCAPE or key == UIKeyEvent.KEY_Q:
                window.set_close_flag()

        window.set_keyboard_callback(keyboard_cb)

        mv_iterator = NonBufferedBiasEventsIterator(input_filename=cli_params["input"], delta_t=4000, bias_file=cli_params["bias"])
        # mv_iterator = BiasEventsIterator(input_filename=cli_params["input"], delta_t=8000, bias_file=cli_params["bias"])
        cam_height_reader, cam_width_reader = mv_iterator.get_size()  # Camera Geometry

        last_frame_produced_time = -1

        assert cam_height_reader == camera_height
        assert cam_width_reader == camera_width

        first_event_time_us = -1
        start_time = time.perf_counter_ns()

        for evs in mv_iterator:
            with pipe.stats_printer.measure_time("main loop"):
                
                # Dispatch system events to the window
                EventLoop.poll_and_dispatch()
                
                if not len(evs):
                    continue
                
                ev_time_diff_ns = (evs["t"][0] - first_event_time_us) * 1000
                proc_time_diff_ns = time.perf_counter_ns() - start_time
                proc_behind = proc_time_diff_ns - ev_time_diff_ns
                
                pipe.stats_printer.add_time_measure_ns("(cpu t - ev[0] t)", proc_behind)
                
                frames_behind_i = int(proc_behind / (1000 * 1000 * 1000 / projector_fps))
                pipe.stats_printer.add_metric("frames behind", frames_behind_i)
                if frames_behind_i > 0 and should_drop_frames:
                    trigger_finder.drop_frame()

                pipe.stats_printer.print_stats_if_needed()
                pipe.stats_printer.count("processed evs", len(evs))

                pos_filter.process_events(evs, pos_events_buf)
                act_filter.process_events(pos_events_buf, act_events_buf)

                trigger_finder.process_events(act_events_buf)
                
                pipe.stats_printer.print_stats_if_needed()

                if window.should_close():
                    pipe.stats_printer.print_stats()
                    sys.exit(0)

        pipe.stats_printer.print_stats()        


if __name__ == "__main__":
    main()
