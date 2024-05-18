const http = require('http');

function checkVolumioStatus(onReady) {
    const options = {
        host: 'localhost',
        port: 3000,
        path: '/api/v1/getState',
        method: 'GET'
    };

    console.log('Checking Volumio status...');

    const request = http.request(options, (res) => {
        let data = '';

        // A chunk of data has been received.
        res.on('data', (chunk) => {
            data += chunk;
        });

        // The whole response has been received.
        res.on('end', () => {
            try {
                const parsedData = JSON.parse(data);
                if (parsedData.status === 'play' || parsedData.status === 'stop' || parsedData.status === 'pause') {
                    console.log('Volumio is ready.');
                    onReady();
                } else {
                    console.log('Volumio is not ready yet. Retrying...');
                    setTimeout(() => checkVolumioStatus(onReady), 5000); // Check again after 5 seconds
                }
            } catch (e) {
                console.log('Error parsing Volumio status. Retrying...');
                setTimeout(() => checkVolumioStatus(onReady), 5000); // Check again after 5 seconds
            }
        });
    });

    request.on('error', (e) => {
        console.error(`Problem with request: ${e.message}`);
        setTimeout(() => checkVolumioStatus(onReady), 5000); // Check again after 5 seconds
    });

    request.end();
}


const { exec } = require('child_process');

// Function to run a script
function runScript(scriptName) {
    console.log(`Executing ${scriptName}.js...`); // Add print statement here
    exec(`node ${scriptName}.js`, (error, stdout, stderr) => {
        if (error) {
            console.error(`Error executing ${scriptName}.js: ${error.message}`);
            return;
        }
        console.log(`${scriptName}.js output: ${stdout}`);
        if (stderr) console.error(`${scriptName}.js stderr: ${stderr}`);
    });
    console.log(`Finished executing ${scriptName}.js`); // Add print statement here
}

// Function to run buttonsleds.js script
function runButtonsLedsScript() {
	console.log("Running buttonsleds.js script...");
    runScript('buttonsleds');
}

// Function to run rotary.js script
function runRotaryScript() {
	console.log("Running rotary.js script...");
    runScript('rotary');
}

// Run both buttonsleds.js and rotary.js on startup
console.log("Starting scripts...");
runButtonsLedsScript();
runRotaryScript();

const os = require("os");
const date = require('date-and-time');
const oled = require('./oled.js');
const fonts = require('./fonts.js');
const fs = require("fs");

var DRIVER;

var TIME_BEFORE_CLOCK = 6000; // in ms
var TIME_BEFORE_SCREENSAVER = 60000; // in ms
var TIME_BEFORE_DEEPSLEEP = 120000; // in ms
var LOGO_DURATION = 15000; // in ms
var CONTRAST = 254; // range 1-254
var extn_exit_sleep_mode = false;


const opts = {
	width: 256,
	height: 64,
	dcPin: 27,
	rstPin : 24,
	contrast : CONTRAST,
	device: "/dev/spidev0.0",
};


var distro = process.argv[2],
supported_distributions = ["volumio","moode"];
if(!distro || !supported_distributions.includes(distro) ){
	console.warn("Unknown target distribution : ",distro, "\nHere are the supported distributions : ", supported_distributions.join() );
}

switch (distro) {
	case 'moode':
	opts.divisor = 32;
	opts.main_rate = 60;
	break;
	case 'volumio':
	opts.divisor = 0xf1;
	opts.main_rate = 40;
	break;
	default:
	opts.divisor = 32;
	opts.main_rate = 60;
}


const REFRESH_TRACK = 20;
var api_state_waiting = false; 

function ap_oled(opts){
	this.scroller_x = 0;
	this.streamerData = {};
	this.ip = null;
	this.height = opts.height;
	this.width = opts.width;
	this.page = null;
    this.data = {
        title : null,
        artist : null,
        album : null,
        volume : null,
        samplerate : null,
        bitdepth : null,
        bitrate : null,
		seek : null,
		duration : null
    };
	this.raw_seek_value = 0;
	this.footertext = "";
	this.update_interval = null;
    this.refresh_track = REFRESH_TRACK;
	this.refresh_action = null;
	this.driver = new oled(opts);
	this.dimmed = false;

}

ap_oled.prototype.volumio_seek_format = function (seek,duration){
	try{
		if(!duration) ratiobar = 0;
		else ratiobar =  ( seek / (duration * 1000) * (this.width - 6) );
	}
	catch(e){
		ratiobar = 0;
	}	
	try{
		duration = new Date(duration * 1000).toISOString().substr(14, 5);
	}
	catch(e){
		duration = "00:00";
	}
	try{
		seek = new Date(seek).toISOString().substr(14, 5);
	}
	catch(e){
		seek = "";
	}
	seek_string = seek + " / "+ duration;
	return({seek_string:seek_string,ratiobar:ratiobar});
}

ap_oled.prototype.moode_seek_format = function (seek,duration,song_percent){
	try{
		if(!duration) ratiobar = 0;
		else ratiobar = (song_percent/100) * (this.width - 6) ;
	}
	catch(e){
		ratiobar = 0;
	}	
	try{
		duration = new Date(duration * 1000).toISOString().substr(14, 5);
	}
	catch(e){
		duration = "00:00";
	}
	try{
		seek = new Date(seek * 1000).toISOString().substr(14, 5);
	}
	catch(e){
		seek = "";
	}
	seek_string = seek + " / "+ duration;
	return({seek_string:seek_string,ratiobar:ratiobar});
}

ap_oled.prototype.listen_to = function(api, frequency) {
    frequency = frequency || 1000;
    let api_caller = null;

    console.log(`Listening to ${api} with frequency ${frequency}ms`);

    if (api === "volumio") {
        var io = require('socket.io-client');
        var socket = io.connect('http://localhost:3000');
        //console.log("Connected to Volumio socket.io server.");

        api_caller = setInterval(() => {
            if (api_state_waiting) return;
            api_state_waiting = true;
            //console.log("Requesting state from Volumio...");
            socket.emit("getState");
        }, frequency);
        let first = true;

        socket.emit("getState"); // Initial state request
        socket.on("pushState", (data) => {
            //console.log("Received pushState from Volumio:", data);

            let exit_sleep = false;
            if (extn_exit_sleep_mode) {
                extn_exit_sleep_mode = false;
                exit_sleep = true;
                //console.log("Exiting sleep mode due to extn_exit_sleep_mode flag.");
            }
            if (first) {
                first = false;
                //console.log("First pushState received, requesting state again for initialization.");
                socket.emit("getState");
                return;
            }
            api_state_waiting = false;

            // Log received data for debugging
            //console.log("Received data from Volumio:", data);

            if (this.data.title !== data.title ||
                this.data.artist !== data.artist ||
                this.data.album !== data.album) {
                //console.log(`Track change detected. Title: ${data.title}, Artist: ${data.artist}, Album: ${data.album}`);
                this.text_to_display = data.title + (data.artist ? " - " + data.artist : "");
                this.driver.CacheGlyphsData(this.text_to_display);
                this.text_width = this.driver.getStringWidthUnifont(this.text_to_display + " - ");

                this.scroller_x = 0;
                this.refresh_track = REFRESH_TRACK;
                this.footertext = "";
                exit_sleep = true;
            }
            // Log volume change for debugging
            if (this.data.volume !== data.volume) {
                //console.log(`Volume change detected. Previous: ${this.data.volume}, New: ${data.volume}`);
                exit_sleep = true;
            }

            let seek_data = this.volumio_seek_format(data.seek, data.duration);

            if (data.status !== "play" && this.raw_seek_value !== data.seek) {
                //console.log("Change in playback position detected.");
                exit_sleep = true;
            }
            this.raw_seek_value = data.seek;

            if (data.status == "play") {
                //console.log("Playback status is 'play'.");
                exit_sleep = true;
            }

            this.footertext = "";
            if (!data.samplerate && !data.bitdepth && !data.bitrate) {
                //console.log("Missing samplerate, bitdepth, or bitrate. Requesting queue to complete the information.");
                socket.emit("getQueue");
            } else {
                //console.log("Formatting footertext with available data.");
                if (data.samplerate) this.footertext += data.samplerate.toString().replace(/\s/gi, "") + " ";
                if (data.bitdepth) this.footertext += data.bitdepth.toString().replace(/\s/gi, "") + " ";
                if (data.bitrate) this.footertext += data.bitrate.toString().replace(/\s/gi, "") + " ";
            }

            //console.log("Formatted footertext for display:", this.footertext);

            this.data = data; // Updating internal state
            this.data.seek_string = seek_data.seek_string;
            this.data.ratiobar = seek_data.ratiobar;

            this.handle_sleep(exit_sleep);

            return api_caller;
        });

        socket.on("pushQueue", (resdata) => {
            //console.log("Received pushQueue from Volumio:", resdata);
            let data = resdata[0];
            if (!this.footertext && data) {
                //console.log("Updating footertext based on queue data.");
                if (data.samplerate) this.footertext += data.samplerate.toString().replace(/\s/gi, "") + " ";
                if (data.bitdepth) this.footertext += data.bitdepth.toString().replace(/\s/gi, "") + " ";
                if (data.bitrate) this.footertext += data.bitrate.toString().replace(/\s/gi, "") + " ";
            }
           //console.log("Updated footertext after receiving queue data:", this.footertext);
        });
    }


	else if( api === "moode" ){
		var moode_listener = require("./moode_listener.js").moode_listener;
		var moode = new moode_listener();
		moode.on("moode_data", (data)=>{
	
			let exit_sleep = false;
			if(extn_exit_sleep_mode){
				extn_exit_sleep_mode = false;
				exit_sleep = true;
			}
			
			api_state_waiting = false;
			
			if( // track change
				this.data.title  !== data.title  || 
				this.data.artist !== data.artist || 
				this.data.album  !== data.album  
			){
				this.text_to_display = data.title + (data.artist?" - " + data.artist:"") + (data.album?" - " + data.album:"");
				this.driver.CacheGlyphsData( this.text_to_display);
				this.text_width = this.driver.getStringWidthUnifont(this.text_to_display + " - ");
				
				this.scroller_x = 0;
				this.refresh_track = REFRESH_TRACK;
				this.footertext = "";
				exit_sleep = true;
			}
			
			// volume change
			if( this.data.volume !== data.volume ){exit_sleep = true;}
			
			// advances in the track
			let seek_data = this.moode_seek_format( data.elapsed, data.time, data.song_percent );
			
			if(data.state !== "play" && this.raw_seek_value !== data.elapsed){
				exit_sleep = true;
			}
			this.raw_seek_value = data.elapsed;
			
			if(data.state == "play"){exit_sleep = true;}
			
			this.footertext = "";
	
			if (data.audio) this.footertext += data.audio + " ";
			if (data.bitrate) this.footertext += data.bitrate + " ";
			
			this.data = data; // Pay attention to the position of this command: once this assignment is made, no further comparison is possible with the previous state
			console.log("Encoded data before setting trackType:", data.encoded);
	
			if (data.encoded) {
				const encodedParts = data.encoded.split(' ');
				// Assuming the format is always "FLAC <bit-depth>/<sample-rate> kHz, <channels>ch"
				// and you only want to keep the "FLAC <bit-depth>/<sample-rate>" part
				const flacInfo = encodedParts.slice(0, 2).join(' '); // This should give you "FLAC 16/48"
				console.log("Modified trackType:", flacInfo);
				this.data.trackType = flacInfo;
			} else {
				this.data.trackType = ""; // Or some default value if necessary
			}
	
			this.data.seek_string = seek_data.seek_string;
			this.data.ratiobar = seek_data.ratiobar;
			this.handle_sleep(exit_sleep);
			this.data.status = data.state;
			console.log("TrackType after being set:", this.data.trackType);
		});
	
		return api_caller;
	}
	else if( api === "ip" ){
		api_caller = setInterval( ()=>{this.get_ip()}, frequency );
		return api_caller;
	}
}	

ap_oled.prototype.snake_screensaver = function(){
if (this.page === "snake_screensaver") return;
	clearInterval(this.update_interval);
	this.page = "snake_screensaver";
	
	let box_pos = [0,0];
	let count = 0;
	let flip = false;
	let tail = [];
	let tail_max = 25;
	let t_tail_length = 1;
	let random_pickups = [];
	let screen_saver_animation_reset =()=>{
		tail = [];
		count = 0;
		t_tail_length = 10;
		random_pickups = [];
		let nb = 7;
		while(nb--){
			let _x =  Math.floor(Math.random() * (this.width ));
			let _y =  Math.floor(Math.random() * (this.height/3))*3;
			random_pickups.push([_x,_y]);
		}
	}
	screen_saver_animation_reset();
	this.refresh_action = ()=>{
		this.driver.buffer.fill(0x00);
		let x;
		if( count % this.width == 0) {flip = !flip}
		if(flip) x = count % this.width +1
		else x = this.width - count % this.width
		let y = ~~( count / this.width ) *3
		tail.push([x,y]);
		if(tail.length > t_tail_length ) tail.shift();
		for(let i of tail){
			this.driver.fillRect(i[0],i[1]-1,2,3,1);
		}
		for(let r of random_pickups){
			if(  ( ( flip && x >= r[0] ) || ( !flip && x <= r[0] ) ) && y >= r[1] ){ 
				t_tail_length +=5;
				random_pickups.splice(random_pickups.indexOf(r),1)
			}
			this.driver.fillRect(r[0],r[1],1,1,1);
		}
		count++;
		this.driver.update(true);
		if(y > this.height ) screen_saver_animation_reset();
	}
	this.update_interval = setInterval( ()=>{this.refresh_action()}, 40);
}

ap_oled.prototype.deep_sleep = function(){
if (this.page === "deep_sleep") return;
	this.status_off = true;
	clearInterval(this.update_interval);
	this.page = "deep_sleep";
	this.driver.turnOffDisplay();

}

ap_oled.prototype.clock_mode = function(){
if (this.page === "clock") return;
        clearInterval(this.update_interval);
        this.page = "clock";

        this.refresh_action = ()=>{
                this.driver.buffer.fill(0x00);
                let fdate = date.format(new Date(),'YYYY/MM/DD'),
                ftime = date.format(new Date(),'HH:mm');

                //this.driver.setCursor(100, 45);
                //this.driver.writeString( fonts.monospace ,2,fdate,4);

                this.driver.setCursor(70,15);
                this.driver.writeString( fonts.monospace ,4,ftime,8);
                //this.driver.drawLine(1, 35, 255, 35, 5, false);

                this.driver.update(true);
        }
        this.refresh_action();
        this.update_interval = setInterval( ()=>{this.refresh_action()}, 1000);

}

ap_oled.prototype.playback_mode = function(){
	if (this.page === "playback") return;
	clearInterval(this.update_interval);

 	this.scroller_x = 0;
	this.page = "playback";
        this.text_to_display = this.text_to_display || "";
	this.refresh_track = REFRESH_TRACK;
	this.refresh_action =()=>{

        if(this.plotting){ return }; // skip plotting of this frame if the pi has not finished plotting the previous frame
        this.plotting = true;

		this.driver.buffer.fill(0x00);

		if(this.data){
            // volume
            if(this.data.volume !== null ){
                let volstring = this.data.volume.toString();
                if(this.data.mute === true || volstring === "0") volstring = "X";

		this.driver.setCursor(4, this.height - 20); // Move volume display down
		this.driver.writeString(fonts.icons, 1, "0", 5); // Volume icon
		this.driver.setCursor(14, this.height - 19); // Adjust accordingly
		this.driver.writeString(fonts.monospace, 1, volstring, 5); // Volume level


            }

		// Repeat Single or Repeat All
	    if(this.data.repeatSingle){
    		this.driver.setCursor(232, this.height - 20); // Move repeat single symbol down
    		this.driver.writeString(fonts.icons, 1, "5", 5); // Repeat single symbol
	    } else if(this.data.repeat){
    		this.driver.setCursor(232, this.height - 20); // Move repeat all symbol down
    		this.driver.writeString(fonts.icons, 1, "4", 5); // Repeat all symbol
	    }

        if (this.data) {
            // Combine trackType and footertext
            let combinedInfo = `${this.data.trackType || ''} ${this.footertext || ''}`.trim();

            // Assuming an average character width for calculation
            let combinedInfoWidth = combinedInfo.length * 6; // Adjust the multiplier based on your font and display
            let startX = (this.width - combinedInfoWidth) / 2; // Calculate X to center the combined string
            let infoYPosition = this.height - 20; // Adjust Y position as needed

            // Check if the method to set cursor and write string is correct for your display library
            this.driver.setCursor(startX, infoYPosition);
            this.driver.writeString(fonts.monospace, 1, combinedInfo, 5);
        }

				//let startX = (this.width - totalTextWidth) / 2;
			
				// Clear the area before drawing new text
				// Assuming there's a method to draw a rectangle, fillRect(x, y, width, height, color)
				// Adjust the height and y position according to your needs
				//this.driver.fillRect(startX, this.height - 30, totalTextWidth, 10, 0); // 0 for black
			
				//this.driver.setCursor(startX, this.height - 21);
				//this.driver.writeString(fonts.monospace, 1, this.data.trackType, 4);
			//}
			
	               //if (this.footertext) {
           		        // Assuming you want the footertext to appear at a specific position, adjust Y as needed
                                //let footertextYPosition = this.height - 10; // Position Y near the bottom of the screen
                                //this.driver.setCursor(0, footertextYPosition);
                               //this.driver.writeString(fonts.monospace, 1, this.footertext, 1);
                       //}		  
			  
			// play pause stop logo
			if(this.data.status){
                let status_symbol = "";
				switch(this.data.status){
					case ("play"):
						status_symbol = "1";
						break;
					case ("pause"):
						status_symbol = "2"
						break;		
					case ("stop"):
						status_symbol = "3"
						break;
				}    

		this.driver.setCursor(246, this.height - 20); // Move play/pause/stop logo down
		this.driver.writeString(fonts.icons, 1, status_symbol, 6);


			}


			// Inside your playback_mode function or similar
			if (this.text_to_display.length) {
				let splitIndex = this.text_to_display.indexOf(" - ");
				let title = this.text_to_display.substring(0, splitIndex);
				let artist = this.text_to_display.substring(splitIndex + 3);

				// Function to handle scrolling or centering text
				const handleTextDisplay = (text, initialY) => {
					let textWidth = this.driver.getStringWidthUnifont(text);
					if (textWidth > this.width) {
						// Scroll text
						if (!this.scrollX) this.scrollX = 0;
						this.driver.cursor_x = this.scrollX;
						this.scrollX = this.scrollX - 1 < -textWidth ? this.width : this.scrollX - 1;
					} else {
						// Center text
						this.driver.cursor_x = (this.width - textWidth) / 2;
					}
					this.driver.cursor_y = initialY;
					this.driver.writeStringUnifont(text, 6);
				};

				// Adjust Y positions as needed
				handleTextDisplay(title, 0); // For title
				handleTextDisplay(artist, 18); // For artist, placed below the title
			}


			// seek data
			if(this.data.seek_string){
				let border_right = this.width -5;
				let Y_seekbar = 35;
				let Ymax_seekbar = 38;
				let bottomY = this.height - 7; // Start 10 pixels from the bottom
				// Adjusted code
				this.driver.drawLine(3, bottomY, border_right, bottomY, 3);
				this.driver.drawLine(border_right, bottomY, border_right, this.height - 4, 3); // Adjusted to be 3 pixels above bottomY
				this.driver.drawLine(3, this.height - 4, border_right, this.height - 4, 3); // Same here
				this.driver.drawLine(3, this.height - 4, 3, bottomY, 3); // And here
				this.driver.fillRect(3, bottomY, this.data.ratiobar, 4, 4); // Filling the progress bar
				this.driver.cursor_y = 43;
				this.driver.cursor_x = 93;
				this.driver.writeString(fonts.monospace , 0 , this.data.seek_string ,5);

			}
		}

		this.driver.update();
		this.plotting = false;
        if(this.refresh_track) return this.refresh_track--; // do not update the scroll cursor before the static frames have elapsed (right after a track change)
		this.scroller_x--;
	}

	this.update_interval = setInterval( ()=>{ this.refresh_action() },opts.main_rate);
	this.refresh_action();
}

ap_oled.prototype.get_ip = function(){
	try{
		let ips = os.networkInterfaces(), ip = "No network.";
		for(a in ips){
			if( ips[a][0]["address"] !== "127.0.0.1" ){
				ip = ips[a][0]["address"];
				break;
			}
		}
		this.ip = ip;
	}
	catch(e){this.ip = null;}
}


ap_oled.prototype.handle_sleep = function(exit_sleep){
	
	if( !exit_sleep ){ // Should the display go into sleep mode?
		
		if(!this.iddle_timeout){ // Check if the screen is not already waiting to go into sleep mode (instruction initiated in a previous cycle).
			
		
			let _deepsleep_ = ()=>{this.deep_sleep();}
		
			let _screensaver_ = ()=>{
				this.snake_screensaver();
				this.iddle_timeout = setTimeout(_deepsleep_,TIME_BEFORE_DEEPSLEEP);
			}
			
			let _clock_ = ()=>{
				this.clock_mode();
				this.iddle_timeout = setTimeout(_screensaver_,TIME_BEFORE_SCREENSAVER);
			}
			
			this.iddle_timeout = setTimeout( _clock_ , TIME_BEFORE_CLOCK );
		}
	}
	else{
		if(this.status_off){
			this.status_off = null;
			this.driver.turnOnDisplay();
		}
		
		if(this.page !== "spdif" ){
			this.playback_mode();
		}

		if(this.iddle_timeout){
			clearTimeout(this.iddle_timeout);
			this.iddle_timeout = null;
		}
	}
}
	
fs.readFile("config.json",(err,data)=>{
	
	if(err) console.log("Cannot read config file. Using default settings instead.");
	else{
		try { 
			data = JSON.parse( data.toString() );
			TIME_BEFORE_SCREENSAVER = (data && data.sleep_after) ? data.sleep_after  * 1000 : TIME_BEFORE_SCREENSAVER
			TIME_BEFORE_DEEPSLEEP = (data && data.deep_sleep_after) ? data.deep_sleep_after  * 1000 : TIME_BEFORE_DEEPSLEEP
			CONTRAST = (data && data.contrast) ? data.contrast : CONTRAST
		} catch(e){
			console.log("Cannot read config file. Using default settings instead.");
		}
	}
	
	opts.contrast = CONTRAST;
	
	
	
	const OLED = new ap_oled(opts);
	var logo_start_display_time = 0;
	
	OLED.driver.begin(
		()=>{
			DRIVER = OLED;
			OLED.driver.load_and_display_logo( (displaylogo)=>{ 
				console.log("logo loaded")
				if(displaylogo) logo_start_display_time = new Date();
			});
			OLED.driver.load_hex_font("unifont.hex", start_app);
		}
	);

	function start_app() {
	    checkVolumioStatus(() => {
	        let time_remaining = 0;
	        if (logo_start_display_time) {
	            time_remaining = LOGO_DURATION - (new Date().getTime() - logo_start_display_time.getTime());
	            time_remaining = (time_remaining <= 0) ? 0 : time_remaining;
	        }
	        setTimeout(() => {
	            OLED.driver.fullRAMclear(() => {
	                OLED.playback_mode();
	                OLED.listen_to(distro, 1000);
	                OLED.listen_to("ip", 1000);
	            });
	        }, time_remaining);
	    });
	}


	function exitcatcher(options) {
		if (options.cleanup) OLED.driver.turnOffDisplay();
		if (options.exit) process.exit();
	}

	process.on('exit', exitcatcher.bind(null,{cleanup:true}));
	process.on('SIGINT', exitcatcher.bind(null, {exit:true}));
	process.on('SIGUSR1', exitcatcher.bind(null, {exit:true}));
	process.on('SIGUSR2', exitcatcher.bind(null, {exit:true}));

});
