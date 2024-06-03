// index.js
const fs = require('fs');
const { checkVolumioStatus } = require('./volumiostatus');
const { runButtonsLedsScript } = require('./utils');
const APOled = require('./oledcontroller');

let TIME_BEFORE_CLOCK = 6000; // in ms
let TIME_BEFORE_SCREENSAVER = 60000; // in ms
let TIME_BEFORE_DEEPSLEEP = 120000; // in ms
const LOGO_DURATION = 15000; // in ms
let CONTRAST = 254; // range 1-254

const opts = {
    width: 256,
    height: 64,
    dcPin: 27,
    rstPin: 24,
    contrast: CONTRAST,
    device: "/dev/spidev0.0",
    divisor: 0xf1,
    main_rate: 40
};

var DRIVER;
var extn_exit_sleep_mode = false;
var currentMode = 'clock'; // Define currentMode globally

// Run both buttonsleds.js and rotary.js on startup
console.log("Starting scripts...");
runButtonsLedsScript();

fs.readFile("config.json", (err, data) => {
    if (err) {
        console.log("Cannot read config file. Using default settings instead.");
    } else {
        try {
            data = JSON.parse(data.toString());
            TIME_BEFORE_SCREENSAVER = (data && data.sleep_after) ? data.sleep_after * 1000 : TIME_BEFORE_SCREENSAVER;
            TIME_BEFORE_DEEPSLEEP = (data && data.deep_sleep_after) ? data.deep_sleep_after * 1000 : TIME_BEFORE_DEEPSLEEP;
            CONTRAST = (data && data.contrast) ? data.contrast : CONTRAST;
        } catch (e) {
            console.log("Cannot read config file. Using default settings instead.");
        }
    }

    opts.contrast = CONTRAST;

    const OLED = new APOled(opts, TIME_BEFORE_CLOCK, TIME_BEFORE_SCREENSAVER, TIME_BEFORE_DEEPSLEEP);
    var logo_start_display_time = 0;

    OLED.driver.begin(() => {
        DRIVER = OLED;
        OLED.driver.load_and_display_logo((displaylogo) => {
            console.log("logo loaded");
            if (displaylogo) logo_start_display_time = new Date();
        });
        OLED.driver.load_hex_font("unifont.hex", start_app);
    });

    function start_app() {
        checkVolumioStatus(() => {
            let time_remaining = 0;
            if (logo_start_display_time) {
                time_remaining = LOGO_DURATION - (new Date().getTime() - logo_start_display_time.getTime());
                time_remaining = (time_remaining <= 0) ? 0 : time_remaining;
            }
            setTimeout(() => {
                OLED.driver.fullRAMclear(() => {
                    OLED.clock_mode();
                    OLED.listen_to("volumio", 1000);
                });
            }, time_remaining);
        });
    }

    function exitcatcher(options) {
        if (options.cleanup) OLED.driver.turnOffDisplay();
        if (options.exit) process.exit();
    }

    process.on('exit', exitcatcher.bind(null, { cleanup: true }));
    process.on('SIGINT', exitcatcher.bind(null, { exit: true }));
    process.on('SIGUSR1', exitcatcher.bind(null, { exit: true }));
    process.on('SIGUSR2', exitcatcher.bind(null, { exit: true }));
});
