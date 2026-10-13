const std = @import("std");
const util = @import("util.zig");

pub fn main() void {
    if (util.ok()) {
        std.debug.print("ok\n", .{});
    }
}
