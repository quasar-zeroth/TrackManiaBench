internal sealed record ClipTiming(int EntityStartMs,int EntityEndMs,int CameraStartMs,int CameraEndMs,int[] MarkerStartsMs)
{
    public static ClipTiming Calculate(int recordEndMs,int finishMs,int prerollMs,int postrollMs)
    {
        if(finishMs<=0||recordEndMs<=0)throw new InvalidDataException("Positive source record end and finish are required.");
        if(prerollMs<0||prerollMs>5000||postrollMs<0||postrollMs>5000)throw new InvalidDataException("Preroll/postroll must be within 0..5000 ms.");
        int end=checked(prerollMs+recordEndMs+postrollMs);
        if(end<prerollMs+finishMs)throw new InvalidDataException("Explicit postroll is required to cover the exact finish when source RecordData ends before finish. No missing samples are invented.");
        var markers=new[]{prerollMs+1000,prerollMs+(recordEndMs/2/20)*20,prerollMs+((recordEndMs-500)/20)*20};
        return new(prerollMs,end,0,end,markers);
    }
    public static int SelfTest()
    {
        int count=0;void Check(bool ok){if(!ok)throw new InvalidDataException("Clip timing self-test failed.");count++;}
        var padded=Calculate(19460,19454,200,200);
        Check(padded.EntityStartMs==200&&padded.EntityEndMs==19860&&padded.CameraStartMs==0&&padded.CameraEndMs==19860);
        Check(padded.MarkerStartsMs.SequenceEqual(new[]{1200,9920,19160}));
        var original=Calculate(19460,19454,0,0);Check(original.EntityStartMs==0&&original.EntityEndMs==19460&&original.MarkerStartsMs.SequenceEqual(new[]{1000,9720,18960}));
        try{Calculate(19460,19454,-1,200);throw new Exception("Negative preroll accepted");}catch(InvalidDataException){count++;}
        var missingTail=Calculate(19760,19790,200,200);Check(missingTail.EntityEndMs==20160);
        try{Calculate(19760,19790,200,0);throw new Exception("Insufficient clip lifetime accepted");}catch(InvalidDataException){count++;}
        var floorQuantized=Calculate(19460,19467,200,200);Check(floorQuantized.EntityEndMs==19860);
        Console.WriteLine($"{count} control-clip timing tests passed. No video/race calibration inferred.");ControlSourceBounds.SelfTest();return 0;
    }
}
